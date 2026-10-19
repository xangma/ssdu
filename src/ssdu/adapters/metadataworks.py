"""Bounded public MetadataWorks metadata traversal, never observation retrieval.

The observed v2 browser protocol has two pagination conventions: inventory uses
one-based ``pageNumber/pageSize``; structural dictionaries use Spring's
zero-based ``page/size``. Repeated content is a coverage gap, never exhaustion.
Inventory page size is configurable. Structural requests use a separate fixed
100-record bound, independent of the inventory batch size and still subject to
the capture job's request, byte, page and entity budgets.
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import quote

from ssdu.metadata_graph import entity_id, make_entity, make_link
from ssdu.models import RetrievalError

_KINDS = frozenset({"inventory", "dataset", "structure", "dictionary", "element"})
STRUCTURAL_PAGE_SIZE = 100
_TASK_KEYS = frozenset(
    {
        "kind",
        "native_id",
        "origin",
        "version",
        "page_number",
        "page_size",
        "dataset_id",
        "table_id",
        "seen_page_hashes",
        "expected_total",
    }
)


def _native(value, label="native ID"):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2000
        or any(ord(char) < 32 for char in value)
    ):
        raise RetrievalError(f"Invalid MetadataWorks {label}")
    return value


def _integer(value, label, minimum=0, maximum=10_000_000):
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise RetrievalError(f"Invalid MetadataWorks {label}")
    return value


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def _object(value, label):
    if not isinstance(value, dict):
        raise RetrievalError(f"MetadataWorks {label} must be an object")
    return value


class MetadataWorksBackend:
    """One HTTP request per task; the durable worker owns pacing and checkpoints."""

    def __init__(self, registration):
        self.registration = registration
        self.catalogue = registration.id

    def initial_tasks(self, page_size=50):
        return [self.validate_task({"kind": "inventory", "page_number": 1, "page_size": page_size})]

    def validate_task(self, task):
        if not isinstance(task, dict) or set(task) - _TASK_KEYS or task.get("kind") not in _KINDS:
            raise RetrievalError("Invalid MetadataWorks task")
        result = dict(task)
        kind = result["kind"]
        paginated = kind in {"inventory", "structure", "dictionary"}
        if paginated:
            result["page_number"] = _integer(
                result.get("page_number", 1 if kind == "inventory" else 0),
                "page number",
                1 if kind == "inventory" else 0,
            )
            result["page_size"] = _integer(result.get("page_size", 100), "page size", 1, 1000)
            hashes = result.get("seen_page_hashes", [])
            if (
                not isinstance(hashes, list)
                or len(hashes) > 10_000
                or any(
                    not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                    for digest in hashes
                )
            ):
                raise RetrievalError("Invalid MetadataWorks page history")
            if len(hashes) != len(set(hashes)):
                raise RetrievalError("Repeated MetadataWorks page history")
            result["seen_page_hashes"] = list(hashes)
            if "expected_total" in result:
                result["expected_total"] = _integer(
                    result["expected_total"], "expected record count"
                )
        elif set(result) & {"page_number", "page_size", "seen_page_hashes", "expected_total"}:
            raise RetrievalError("Unpaginated MetadataWorks task has pagination fields")
        if kind == "inventory":
            if set(result) - {
                "kind",
                "page_number",
                "page_size",
                "seen_page_hashes",
                "expected_total",
            }:
                raise RetrievalError("Inventory tasks cannot select a dataset")
        else:
            for key in ("native_id", "origin"):
                result[key] = _native(result.get(key), key)
            # Historical version routes have not been verified. A version label
            # must never relabel metadata returned by an unversioned endpoint.
            if result.get("version") is not None:
                raise RetrievalError("Historical MetadataWorks detail retrieval is not verified")
            result["version"] = None
            if kind == "element":
                result["dataset_id"] = _native(result.get("dataset_id"), "dataset ID")
                if result.get("table_id") is not None:
                    result["table_id"] = _native(result["table_id"], "table ID")
            elif set(result) & {"dataset_id", "table_id"}:
                raise RetrievalError("Dataset tasks cannot select an element parent")
        return result

    def _entity(self, kind, native_id, task, resource, pointer="", **fields):
        fields["field_status"] = {
            "version": "mapped" if task.get("version") is not None else "not_provided",
            **fields.get("field_status", {}),
        }
        return make_entity(
            self.catalogue,
            task.get("origin", "catalogue"),
            kind,
            _native(native_id),
            version=task.get("version"),
            provenance=self._provenance(resource, pointer),
            **fields,
        )

    @staticmethod
    def _provenance(resource, pointer=""):
        return [
            {
                "url": resource.url,
                "body_sha256": hashlib.sha256(resource.body).hexdigest(),
                "pointer": pointer,
                "captured_at": resource.captured_at,
                "checked_at": resource.checked_at,
            }
        ]

    def _id(self, kind, native_id, task, version=None):
        return entity_id(
            self.catalogue,
            task.get("origin", "catalogue"),
            kind,
            _native(native_id),
            task.get("version") if version is None else version,
        )

    def _link(self, result, source, target, relation, resource, pointer=""):
        result["links"].append(
            make_link(source, target, relation, provenance=self._provenance(resource, pointer))
        )

    def _coverage(self, result, task, resource, kind, status, observed=0, expected=None, reason=""):
        result["coverage"].append(
            {
                "scope": ":".join(
                    [task["kind"], task.get("origin", "catalogue"), task.get("native_id", "all")]
                ),
                "kind": kind,
                "status": status,
                "observed": observed,
                "expected": expected,
                "reason": reason,
                "provenance": self._provenance(resource),
            }
        )

    def fetch(self, task, client):
        task = self.validate_task(task)
        kind = task["kind"]
        params = {}
        if kind == "inventory":
            path = "/dataset"
            params = {
                "include": "dataset",
                "pageSize": task["page_size"],
                "pageNumber": task["page_number"],
            }
        else:
            native = quote(task["native_id"], safe="")
            prefix = {
                "dataset": "/dataset/",
                "structure": "/structuralMetadata/",
                "dictionary": "/structuralMetadata/dataDictionary/",
                "element": "/dataElement/",
            }[kind]
            path = prefix + native
            params["origin" if kind == "element" else "originId"] = task["origin"]
            if kind in {"structure", "dictionary"}:
                params.update(page=task["page_number"], size=task["page_size"])
        resource = client.get(self.registration.api_base + path, params=params)
        data = _object(resource.json(), "response")
        result = {"entities": [], "links": [], "next_tasks": [], "coverage": []}
        if kind in {"inventory", "structure", "dictionary"}:
            content = self._page(data, task, resource, result)
            if content is None:
                return result
        if kind == "inventory":
            self._inventory(content, task, resource, result)
        elif kind == "dataset":
            self._dataset(data, task, resource, result)
        elif kind == "structure":
            self._structure(content, task, resource, result)
        elif kind == "dictionary":
            self._dictionary(content, task, resource, result)
        else:
            self._element(data, task, resource, result)
        # Canonical copies ensure checkpoint tasks cannot mutate source state.
        result["next_tasks"] = [self.validate_task(item) for item in result["next_tasks"]]
        return result

    def _page(self, data, task, resource, result):
        inventory = task["kind"] == "inventory"
        total = data.get("totalCount" if inventory else "totalSize")
        if total is not None:
            total = _integer(total, "declared record count")
        if "expected_total" in task and total != task["expected_total"]:
            raise RetrievalError("MetadataWorks record count changed during pagination")
        identity = data.get("resourceIdentifier")
        if not inventory and identity is not None:
            identity = _object(identity, "resource identifier")
            if (
                _native(identity.get("id")) != task["native_id"]
                or _native(identity.get("originId"), "origin ID") != task["origin"]
            ):
                raise RetrievalError("MetadataWorks page identifies a different dataset or origin")
        content = data.get("content")
        if content is None and total == 0:
            content = []
        if not isinstance(content, list) or any(not isinstance(row, dict) for row in content):
            raise RetrievalError("MetadataWorks catalogue content must be an object list")
        page_key = "currentPage" if inventory else "pageNumber"
        observed_page = data.get(page_key)
        if observed_page is not None and observed_page != task["page_number"]:
            raise RetrievalError("MetadataWorks returned a different page than requested")
        size = data.get("pageSize" if inventory else "size", task["page_size"])
        bounded_unpaginated_structure = (
            task["kind"] == "structure"
            and size == -1
            and task["page_number"] == 0
            and total is not None
            and total <= task["page_size"]
            and len(content) == total
        )
        if size != task["page_size"] and not bounded_unpaginated_structure:
            raise RetrievalError("MetadataWorks ignored the bounded page size")
        if len(content) > task["page_size"]:
            raise RetrievalError("MetadataWorks returned more records than the page bound")
        if "numberOfElements" in data and data["numberOfElements"] != len(content):
            raise RetrievalError("MetadataWorks page record count does not match content")
        identities = [
            (
                _native(row.get("id")),
                json.dumps(
                    _native(_object(row.get("origin"), "dataset origin").get("id"))
                    if inventory
                    else row.get("parentId")
                ),
            )
            for row in content
        ]
        if len(identities) != len(set(identities)):
            raise RetrievalError("MetadataWorks page repeats a native record")
        fingerprint = hashlib.sha256(
            json.dumps(sorted(identities), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        entity_kind = {"inventory": "dataset", "structure": "table", "dictionary": "variable"}[
            task["kind"]
        ]
        if content and fingerprint in task["seen_page_hashes"]:
            self._coverage(
                result,
                task,
                resource,
                entity_kind,
                "partial",
                expected=total,
                reason="Provider repeated earlier page content; traversal stopped without declaring completeness",
            )
            return None
        index = task["page_number"] - (1 if inventory else 0)
        offset = index * task["page_size"]
        pages = data.get("totalPages")
        if pages is not None:
            pages = _integer(pages, "page count", -1)
        if total is not None:
            more = offset + len(content) < total
            if (more and len(content) != task["page_size"]) or offset + len(content) > total:
                raise RetrievalError("MetadataWorks page content contradicts its declared total")
        elif pages is not None and pages >= 0:
            more = index + 1 < pages
            if more and not content:
                raise RetrievalError(
                    "MetadataWorks returned an empty page before catalogue exhaustion"
                )
        else:
            more = len(content) == task["page_size"]
        if more:
            result["next_tasks"].append(
                {
                    **task,
                    "page_number": task["page_number"] + 1,
                    "seen_page_hashes": [*task["seen_page_hashes"], fingerprint],
                    **({"expected_total": total} if total is not None else {}),
                }
            )
        status = "page" if more else "exhausted"
        self._coverage(
            result,
            task,
            resource,
            entity_kind,
            status,
            len(content),
            total,
            "Public endpoint scope only; absent metadata and hidden holdings have no inferred denominator",
        )
        return content

    def _inventory(self, content, task, resource, result):
        catalogue = self._entity(
            "catalogue",
            self.catalogue,
            task,
            resource,
            label=self.catalogue,
            attributes={
                "scope": self.registration.scope,
                "catalogue_url": self.registration.catalogue_url,
                "family": "metadataworks",
                "api_version": self.registration.api_version,
                "observation_retrieval": "unsupported",
            },
        )
        result["entities"].append(catalogue)
        seen = set()
        for row in content:
            native_id = _native(row.get("id"))
            origin = _object(row.get("origin"), "dataset origin")
            origin_id = _native(origin.get("id"), "origin ID")
            identity = (native_id, origin_id)
            if identity in seen:
                raise RetrievalError("MetadataWorks inventory repeats a dataset within one page")
            seen.add(identity)
            result["next_tasks"].append(
                {"kind": "dataset", "native_id": native_id, "origin": origin_id, "version": None}
            )

    @staticmethod
    def _check_identity(data, task):
        if _native(data.get("id")) != task["native_id"]:
            raise RetrievalError("MetadataWorks detail identifies a different native record")
        origin = data.get("origin")
        if origin is not None and _native(_object(origin, "origin").get("id")) != task["origin"]:
            raise RetrievalError("MetadataWorks detail identifies a different origin")
        if "originId" in data and _native(data["originId"], "origin ID") != task["origin"]:
            raise RetrievalError("MetadataWorks detail identifies a different origin")

    def _dataset(self, data, task, resource, result):
        self._check_identity(data, task)
        summary = _object(data.get("summary", {}), "dataset summary")
        documentation = _object(data.get("documentation", {}), "dataset documentation")
        description = _text(documentation.get("description")) or _text(summary.get("abstract"))
        attributes = {
            key: data[key]
            for key in (
                "origin",
                "summary",
                "coverage",
                "provenance",
                "accessAndGovernance",
                "enrichmentAndLinkage",
                "dataStatus",
                "status",
                "issued",
                "lastModified",
                "descriptiveSchema",
                "version",
                "semanticVersion",
                "modelVersion",
            )
            if key in data
        }
        attributes["documentation"] = documentation
        attributes["observation_retrieval"] = "unsupported"
        dataset = self._entity(
            "dataset",
            task["native_id"],
            task,
            resource,
            label=_text(summary.get("title")) or _text(data.get("title")) or task["native_id"],
            description=description,
            attributes=attributes,
            field_status={"description": "mapped" if description is not None else "not_provided"},
        )
        result["entities"].append(dataset)
        catalogue_id = entity_id(self.catalogue, "catalogue", "catalogue", self.catalogue)
        self._link(result, catalogue_id, dataset["id"], "has_dataset", resource)
        history = data.get("versionHistory", [])
        if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
            raise RetrievalError("MetadataWorks version history must be an object list")
        seen = set()
        for index, release in enumerate(history):
            version = _native(release.get("semanticVersion"), "release version")
            native = _native(release.get("dataModelId", task["native_id"]), "release ID")
            if (native, version) in seen:
                raise RetrievalError("MetadataWorks repeats a release descriptor")
            seen.add((native, version))
            release_entity = self._entity(
                "release",
                native,
                {**task, "version": version},
                resource,
                f"/versionHistory/{index}",
                label=version,
                description=_text(release.get("revisionNotes")),
                attributes={**release, "capture_depth": "descriptor_only"},
            )
            result["entities"].append(release_entity)
            self._link(
                result,
                dataset["id"],
                release_entity["id"],
                "has_release",
                resource,
                f"/versionHistory/{index}",
            )
        if history:
            self._coverage(
                result,
                task,
                resource,
                "release",
                "partial",
                len(history),
                len(history),
                "Published release descriptors retained; historical detail/dictionary routes are not verified",
            )
        self._coverage(
            result,
            task,
            resource,
            "release",
            "unknown",
            reason="Current detail and dictionary endpoints are unversioned; unknown entity versions remain null rather than inheriting a historical release label",
        )
        for kind in ("structure", "dictionary"):
            result["next_tasks"].append(
                {
                    "kind": kind,
                    "native_id": task["native_id"],
                    "origin": task["origin"],
                    "version": None,
                    "page_number": 0,
                    "page_size": STRUCTURAL_PAGE_SIZE,
                }
            )
        # Public attachment links are metadata. They are not automatically
        # downloaded: dictionaries and observation files can share asset routes.
        if data.get("assets") or documentation.get("associatedMedia"):
            self._coverage(
                result,
                task,
                resource,
                "variable",
                "partial",
                reason="Linked files may contain additional dictionaries; attachment parsing is not implemented",
            )
        if "assets" in data:
            dataset["attributes"]["assets"] = data["assets"]

    def _structure(self, content, task, resource, result):
        dataset_id = self._id("dataset", task["native_id"], task)

        def visit(row, pointer, parent, depth=0):
            if depth > 32:
                raise RetrievalError("MetadataWorks table hierarchy exceeds depth bound")
            native_id = _native(row.get("id"), "table ID")
            table = self._entity(
                "table",
                native_id,
                task,
                resource,
                pointer,
                label=_text(row.get("name")) or _text(row.get("label")) or native_id,
                description=_text(row.get("description")),
                attributes={key: row[key] for key in ("elementType", "childClasses") if key in row},
            )
            result["entities"].append(table)
            self._link(result, parent, table["id"], "has_table", resource, pointer)
            children = row.get("dataClasses", [])
            if not isinstance(children, list) or any(
                not isinstance(child, dict) for child in children
            ):
                raise RetrievalError("MetadataWorks child classes must be an object list")
            for index, child in enumerate(children):
                visit(child, f"{pointer}/dataClasses/{index}", table["id"], depth + 1)
            if row.get("childClasses") is True and not children:
                self._coverage(
                    result,
                    task,
                    resource,
                    "table",
                    "partial",
                    reason="A table declares child classes without embedded class records; a separate child-class route has not been verified",
                )
            elements = row.get("dataElements", [])
            if not isinstance(elements, list) or any(
                not isinstance(item, dict) for item in elements
            ):
                raise RetrievalError("MetadataWorks table variables must be an object list")
            for index, element in enumerate(elements):
                self._schedule_element(
                    result,
                    element,
                    task,
                    resource,
                    f"{pointer}/dataElements/{index}",
                    native_id,
                )

        for index, row in enumerate(content):
            visit(row, f"/content/{index}", dataset_id)

    def _schedule_element(self, result, row, task, resource, pointer, table_id=None):
        item = {
            "kind": "element",
            "native_id": _native(row.get("id"), "variable ID"),
            "origin": task["origin"],
            "version": None,
            "dataset_id": task["native_id"],
        }
        if (
            table_id is not None
            and row.get("parentId") is not None
            and _native(row["parentId"], "table ID") != table_id
        ):
            raise RetrievalError("MetadataWorks variable declares a different containing table")
        table_id = row.get("parentId", table_id)
        if table_id is not None:
            item["table_id"] = _native(table_id, "table ID")
        result["next_tasks"].append(item)
        parent_kind = "table" if table_id is not None else "dataset"
        parent_native = table_id if table_id is not None else task["native_id"]
        self._link(
            result,
            self._id(parent_kind, parent_native, task),
            self._id("variable", item["native_id"], task),
            "has_variable",
            resource,
            pointer,
        )

    def _dictionary(self, content, task, resource, result):
        for index, row in enumerate(content):
            self._schedule_element(result, row, task, resource, f"/content/{index}")

    def _element(self, data, task, resource, result):
        self._check_identity(data, task)
        dtype = data.get("dataType")
        attributes = {
            key: data[key]
            for key in (
                "elementType",
                "issued",
                "lastModified",
                "unit",
                "units",
                "format",
                "missingValues",
                "missingValueCodes",
            )
            if key in data
        }
        if isinstance(dtype, dict):
            attributes["data_type"] = {
                key: value for key, value in dtype.items() if key not in {"enumerations", "count"}
            }
        elif dtype is not None:
            attributes["data_type"] = dtype
        variable = self._entity(
            "variable",
            task["native_id"],
            task,
            resource,
            label=_text(data.get("name")) or task["native_id"],
            description=_text(data.get("description")),
            attributes=attributes,
            field_status={
                "description": "mapped"
                if _text(data.get("description")) is not None
                else "not_provided",
                "questions": "not_provided",
                "code_list": "not_provided",
            },
        )
        result["entities"].append(variable)
        self._questions(data, task, resource, result, variable)
        if isinstance(dtype, dict) and "enumerations" in dtype:
            self._codes(dtype, task, resource, result, variable)
        self._coverage(
            result,
            task,
            resource,
            "question",
            "unknown",
            reason="Only explicitly published question fields are mapped; no question-inventory denominator is available",
        )

    def _questions(self, data, task, resource, result, variable):
        entries = []
        if "question" in data:
            entries.append((data["question"], "/question"))
        if "questions" in data:
            values = data["questions"]
            if not isinstance(values, list):
                raise RetrievalError("MetadataWorks questions must be an array")
            entries.extend((value, f"/questions/{index}") for index, value in enumerate(values))
        if "questionText" in data:
            entries.append(
                ({"text": data["questionText"], "id": data.get("questionId")}, "/questionText")
            )
        for value, pointer in entries:
            if value is None:
                continue
            if isinstance(value, str):
                value = {"text": value}
            value = _object(value, "question")
            wording = _text(value.get("text")) or _text(value.get("questionText"))
            native = value.get("id", value.get("identifier"))
            supplied = native is not None
            if not supplied and wording is None:
                raise RetrievalError("MetadataWorks question supplies neither identity nor text")
            native = _native(native) if supplied else f"element/{task['native_id']}{pointer}"
            question = self._entity(
                "question",
                native,
                task,
                resource,
                pointer,
                label=wording or native,
                attributes={
                    "text": wording,
                    "identity_basis": "source_identifier" if supplied else "source_path",
                    "source_native_id": native if supplied else None,
                },
                field_status={"wording": "mapped" if wording is not None else "not_provided"},
            )
            result["entities"].append(question)
            self._link(result, variable["id"], question["id"], "has_question", resource, pointer)
            variable["field_status"]["questions"] = "mapped"

    def _codes(self, dtype, task, resource, result, variable):
        values = dtype["enumerations"]
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise RetrievalError("MetadataWorks enumerations must be an object array")
        native = f"element/{task['native_id']}/dataType/enumerations"
        codes = self._entity(
            "code_list",
            native,
            task,
            resource,
            "/dataType/enumerations",
            label=_text(dtype.get("name")) or native,
            attributes={"identity_basis": "source_path", "source_native_id": dtype.get("id")},
        )
        result["entities"].append(codes)
        self._link(
            result, variable["id"], codes["id"], "has_code_list", resource, "/dataType/enumerations"
        )
        variable["field_status"]["code_list"] = "mapped"
        seen = set()
        for index, value in enumerate(values):
            code_value = value.get("name", value.get("code", value.get("value")))
            if not isinstance(code_value, (str, int, float, bool)) or code_value is None:
                raise RetrievalError("MetadataWorks enumeration requires an explicit scalar code")
            identity = json.dumps(
                code_value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            )
            if identity in seen:
                raise RetrievalError("MetadataWorks repeats an enumeration code")
            seen.add(identity)
            code_native = f"{native}/code/{identity}"
            pointer = f"/dataType/enumerations/{index}"
            code = self._entity(
                "code",
                code_native,
                task,
                resource,
                pointer,
                label=_text(value.get("description"))
                or _text(value.get("label"))
                or str(code_value),
                attributes={
                    "value": code_value,
                    **{
                        key: value[key]
                        for key in (
                            "description",
                            "label",
                            "isMissing",
                            "missingValue",
                            "missingReason",
                            "meaning",
                        )
                        if key in value
                    },
                },
            )
            result["entities"].append(code)
            self._link(result, codes["id"], code["id"], "has_code", resource, pointer)
