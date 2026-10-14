"""Persistent, versioned discovery metadata. Importing retained evidence is offline."""

import fcntl
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from ssdu.models import RetrievalError
from ssdu.providers import DEFAULT_PROVIDERS
from ssdu.store import PROVIDERS, adapter, canonical


def now():
    return datetime.now(UTC).isoformat()


def dataset_key(provider, native_id):
    return f"{provider}:{native_id}"


def descriptor(manifest, metadata, storage_types=None):
    columns = []
    labels = {v: k for k, v in metadata.get("column_map", {}).items()}
    definitions = {
        "lsoa_code": ("Identifier for a 2021 Lower-layer Super Output Area", "GSS code"),
        "lsoa_name": ("Name of a 2021 Lower-layer Super Output Area", "name"),
        "local_authority_code": ("2024 Local Authority District identifier", "GSS code"),
        "local_authority_name": ("2024 Local Authority District name", "name"),
        "imd_score": ("Index of Multiple Deprivation score; higher means more deprived", "score"),
        "imd_rank": ("England-wide rank; 1 is the most deprived LSOA", "rank"),
        "imd_decile": (
            "England-wide decile; 1 is the most deprived 10% of LSOAs, 10 the least deprived",
            "decile",
        ),
    }
    if manifest["provider"] != "deprivation":
        definitions = {}
    supplied = metadata.get("columns", {}) if manifest["provider"] == "local" else {}
    for name, dtype in manifest.get("columns", {}).items():
        definition, unit = definitions.get(name, (labels.get(name, name), None))
        column = supplied.get(name, {})
        columns.append(
            {
                "name": name,
                "dtype": dtype,
                "storage_dtype": (storage_types or {}).get(name),
                "label": column.get("label") or labels.get(name, name),
                "definition": column.get("definition") or definition,
                "unit": column.get("unit", unit),
                "role": "identifier"
                if name in definitions and name not in {"imd_score", "imd_rank", "imd_decile"}
                else "unspecified",
            }
        )
    return {
        "dataset_id": dataset_key(manifest["provider"], manifest["dataset_id"]),
        "native_id": manifest["dataset_id"],
        "provider": manifest["provider"],
        "title": manifest["title"],
        "description": metadata.get(
            "description", metadata.get("comparability", manifest["title"])
        ),
        "source_url": manifest["source_url"],
        "licence": manifest["licence"],
        "citation": manifest["citation"],
        "release": manifest["release"],
        "columns": columns,
        "metadata_scope": "Retained snapshot metadata; provider-wide harvest not performed",
        "source_metadata": metadata,
    }


def snapshot_descriptor(store, manifest):
    path = store.path(manifest["product_id"])
    metadata = json.loads((path / "metadata.json").read_bytes())
    storage_types = {field.name: str(field.type) for field in pq.read_schema(path / "data.parquet")}
    return descriptor(manifest, metadata, storage_types)


class Registry:
    def __init__(self, root="data"):
        self.path = Path(root).resolve() / "registry.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT PRIMARY KEY, provider TEXT NOT NULL, native_id TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL,
                    metadata_version TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata_versions (
                    version TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS products (
                    snapshot_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, kind TEXT NOT NULL,
                    created_at TEXT NOT NULL, rows INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY, dataset_id TEXT NOT NULL, checked_at TEXT NOT NULL,
                    status TEXT NOT NULL, snapshot_id TEXT, provider_contact INTEGER NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS integrity (
                    snapshot_id TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS dataset_search USING fts5(
                    dataset_id UNINDEXED, title, description, metadata_text
                );
                CREATE TABLE IF NOT EXISTS catalogue_imports (
                    snapshot_id TEXT PRIMARY KEY, report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalogue_versions (
                    snapshot_id TEXT NOT NULL, dataset_id TEXT NOT NULL, version TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, dataset_id)
                );
            """)
            fields = {r[1] for r in db.execute("PRAGMA table_info(datasets)")}
            if "current_snapshot_id" not in fields:
                db.execute("ALTER TABLE datasets ADD COLUMN current_snapshot_id TEXT")
        self.seed()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _upsert(self, db, document, *, set_current=True):
        version = hashlib.sha256(canonical(document)).hexdigest()
        key = document["dataset_id"]
        db.execute(
            "INSERT OR IGNORE INTO metadata_versions VALUES (?,?,?)",
            (version, key, canonical(document).decode()),
        )
        if not set_current:
            return version
        db.execute(
            """INSERT INTO datasets
            (dataset_id,provider,native_id,title,description,metadata_version,updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(dataset_id) DO UPDATE SET title=excluded.title,
            description=excluded.description, metadata_version=excluded.metadata_version,
            updated_at=excluded.updated_at""",
            (
                key,
                document["provider"],
                document["native_id"],
                document["title"],
                document["description"],
                version,
                now(),
            ),
        )
        db.execute("DELETE FROM dataset_search WHERE dataset_id=?", (key,))
        db.execute(
            "INSERT INTO dataset_search VALUES (?,?,?,?)",
            (key, document["title"], document["description"], canonical(document).decode()),
        )
        return version

    def seed(self):
        """Three curated entries; seed presence makes no assertion about materialisation."""
        with self.connect() as db:
            for provider in DEFAULT_PROVIDERS:
                request = adapter(provider).default_request()
                key = dataset_key(provider, request.dataset_id)
                if db.execute("SELECT 1 FROM datasets WHERE dataset_id=?", (key,)).fetchone():
                    continue
                titles = {
                    "nomis": "Claimant count",
                    "statswales": "Relative income poverty by family type",
                    "deprivation": "English indices of deprivation 2025 — LSOA ranks and scores",
                }
                self._upsert(
                    db,
                    {
                        "dataset_id": key,
                        "native_id": request.dataset_id,
                        "provider": provider,
                        "title": titles[provider],
                        "description": titles[provider],
                        "columns": [],
                        "metadata_scope": "Curated seed; no provider-wide harvest",
                        "release": {},
                        "licence": None,
                        "citation": None,
                        "source_metadata": {},
                    },
                )

    def retain_metadata(self, document, *, db=None):
        """Retain a pinned interpretation without changing a dataset's current version."""
        with self.connect() if db is None else nullcontext(db) as connection:
            return self._upsert(connection, document, set_current=False)

    def import_catalogue(self, evidence_store, snapshot_id):
        """Atomically index a verified provider harvest, preserving materialised metadata.

        The immutable harvest remains authoritative; SQLite is a rebuildable index.
        Re-importing the same snapshot is idempotent. No data products are invented.
        """
        import copy

        evidence = evidence_store.read(snapshot_id)
        descriptors = evidence.get("catalogue_descriptors")
        if not isinstance(descriptors, list):
            raise TypeError("Snapshot has no provider catalogue descriptors")
        datasets = {row["id"]: row for row in evidence["datasets"]}
        sources = {row["id"] for row in evidence["sources"]}
        documents, keys = [], set()
        for row in descriptors:
            if not isinstance(row, dict):
                raise TypeError("Catalogue descriptor must be an object")
            document = copy.deepcopy(row)
            key = document.get("dataset_id")
            provider, native = document.get("provider"), document.get("native_id")
            if (
                provider not in PROVIDERS
                or not isinstance(native, str)
                or not native
                or key != dataset_key(provider, native)
                or key in keys
                or key not in datasets
            ):
                raise ValueError("Invalid or duplicate provider catalogue identity")
            if any(not isinstance(document.get(f), str) for f in ("title", "description")):
                raise TypeError("Catalogue title/description must be strings")
            if document["title"] != datasets[key]["title"]:
                raise ValueError("Catalogue title differs from retained dataset")
            refs = document.get("references")
            if (
                not isinstance(refs, list)
                or not refs
                or any(not isinstance(ref, dict) or ref.get("id") not in sources for ref in refs)
            ):
                raise ValueError("Catalogue descriptor must retain source references")
            if sorted(refs, key=lambda ref: ref["id"]) != datasets[key]["references"]:
                raise ValueError("Catalogue references differ from retained dataset")
            if not isinstance(document.get("columns", []), list):
                raise TypeError("Catalogue columns must be a list")
            keys.add(key)
            document["harvest_snapshot_id"] = snapshot_id
            documents.append(document)
        with self.connect() as db:
            previous = db.execute(
                "SELECT report FROM catalogue_imports WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if previous:
                return {**json.loads(previous[0]), "reused": True}
            preserved = 0
            for document in documents:
                existing = db.execute(
                    """SELECT d.current_snapshot_id,m.document FROM datasets d
                    JOIN metadata_versions m ON d.metadata_version=m.version WHERE d.dataset_id=?""",
                    (document["dataset_id"],),
                ).fetchone()
                current = json.loads(existing["document"]) if existing else None
                replace = existing is None or (
                    existing["current_snapshot_id"] is None
                    and (
                        "harvest_snapshot_id" in current
                        or current.get("metadata_scope", "").startswith("Curated seed;")
                    )
                )
                version = self._upsert(db, document, set_current=replace)
                preserved += int(not replace)
                db.execute(
                    "INSERT INTO catalogue_versions VALUES (?,?,?)",
                    (snapshot_id, document["dataset_id"], version),
                )
            result = {
                "snapshot_id": snapshot_id,
                "indexed": len(documents),
                "preserved_current_metadata": preserved,
                "reused": False,
                "coverage": evidence.get("coverage_report", {}),
            }
            db.execute(
                "INSERT INTO catalogue_imports VALUES (?,?)", (snapshot_id, canonical(result))
            )
        return result

    def register(
        self,
        store,
        manifest,
        *,
        make_current=False,
        preserve_current=False,
        db=None,
        _publication_locked=False,
    ):
        publication = (
            nullcontext()
            if db is not None or _publication_locked
            else (store.root / ".publication.lock").open("a")
        )
        with publication as handle:
            if handle is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            return self._register(
                store, manifest, make_current=make_current, preserve_current=preserve_current, db=db
            )

    def _register(self, store, manifest, *, make_current=False, preserve_current=False, db=None):
        manifest = store.verify(manifest["product_id"])
        document = snapshot_descriptor(store, manifest)
        version = hashlib.sha256(canonical(document)).hexdigest()
        key = dataset_key(manifest["provider"], manifest["dataset_id"])
        with self.connect() if db is None else nullcontext(db) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO integrity VALUES (?,?,?)",
                (manifest["product_id"], "verified", now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata_versions VALUES (?,?,?)",
                (version, key, canonical(document).decode()),
            )
            existing = connection.execute(
                "SELECT 1 FROM products WHERE snapshot_id=?", (manifest["product_id"],)
            ).fetchone()
            record = connection.execute(
                "SELECT current_snapshot_id,metadata_version FROM datasets WHERE dataset_id=?",
                (key,),
            ).fetchone()
            if existing and not make_current:
                if (
                    not preserve_current
                    and record
                    and record[0] == manifest["product_id"]
                    and record[1] != version
                ):
                    self._upsert(connection, document)
                return
            if existing and record and record[0] == manifest["product_id"] and record[1] == version:
                return
            sources = connection.execute(
                "SELECT created_at FROM products WHERE dataset_id=? AND kind='source'", (key,)
            ).fetchall()
            current = (
                make_current
                or not sources
                or (
                    manifest.get("kind") == "source"
                    and manifest["created_at"] >= max(s[0] for s in sources)
                )
            )
            if preserve_current and record and record[0]:
                current = False
            self._upsert(connection, document, set_current=current)
            if current:
                connection.execute(
                    "UPDATE datasets SET current_snapshot_id=? WHERE dataset_id=?",
                    (manifest["product_id"], key),
                )
            connection.execute(
                "INSERT OR IGNORE INTO products VALUES (?,?,?,?,?)",
                (
                    manifest["product_id"],
                    key,
                    manifest.get("kind", "selection"),
                    manifest["created_at"],
                    manifest["rows"],
                ),
            )

    def import_store(self, store):
        # Indexing cannot commit references to an import's uncommitted captures.
        with (store.root / ".publication.lock").open("a") as publication:
            fcntl.flock(publication, fcntl.LOCK_EX)
            return self._import_store(store)

    def _import_store(self, store):
        manifests = []
        self.last_import_errors = []
        for path in sorted(store.snapshots.iterdir()):
            if not path.is_dir() or not re.fullmatch(r"[0-9a-f]{64}", path.name):
                continue
            try:
                manifest = json.loads((path / "manifest.json").read_bytes())
                if not isinstance(manifest, dict) or manifest.get("product_id") != path.name:
                    raise ValueError("Invalid manifest identity")
                if not isinstance(manifest.get("created_at"), str):
                    raise TypeError("Invalid capture timestamp")
                manifests.append(manifest)
            except (OSError, ValueError, KeyError, TypeError):
                self._quarantine(path.name)
        manifests.sort(key=lambda m: m["created_at"])
        imported = 0
        for manifest in manifests:
            try:
                self.register(store, manifest, _publication_locked=True)
                imported += 1
            except (RetrievalError, OSError, ValueError, KeyError, TypeError):
                self._quarantine(manifest.get("product_id", "unknown"))
        source_index = store.root / "sources.json"
        if source_index.exists():
            for key, snapshot_id in json.loads(source_index.read_bytes()).items():
                try:
                    manifest = store.verify(snapshot_id)
                except (RetrievalError, OSError, ValueError, KeyError, TypeError):
                    self._quarantine(snapshot_id)
                    continue
                if (
                    manifest.get("kind") != "source"
                    or dataset_key(manifest["provider"], manifest["dataset_id"]) != key
                ):
                    raise ValueError("Source index points to a different dataset or selection")
                self.register(store, manifest, make_current=True, _publication_locked=True)
        return imported

    def _quarantine(self, snapshot_id):
        """Report unusable captures individually; retain unaffected catalogue entries."""
        self.last_import_errors.append(
            {"snapshot_id": snapshot_id, "status": "verification_failed"}
        )
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO integrity VALUES (?,?,?)",
                (snapshot_id, "verification_failed", now()),
            )

    def check(self, dataset_id, status, snapshot_id=None, *, provider_contact=False, message=""):
        with self.connect() as db:
            db.execute(
                """INSERT INTO checks
                (dataset_id,checked_at,status,snapshot_id,provider_contact,message) VALUES (?,?,?,?,?,?)""",
                (dataset_id, now(), status, snapshot_id, int(provider_contact), message),
            )

    def resolve(self, dataset_id):
        with self.connect() as db:
            matches = db.execute(
                "SELECT dataset_id FROM datasets WHERE dataset_id=? OR native_id=?",
                (dataset_id, dataset_id),
            ).fetchall()
        if not matches:
            raise KeyError(dataset_id)
        if len(matches) != 1:
            raise ValueError("Dataset not found or identifier ambiguous")
        return matches[0][0]

    def get(self, dataset_id):
        key = self.resolve(dataset_id)
        with self.connect() as db:
            record = db.execute(
                """SELECT d.*,m.document FROM datasets d JOIN metadata_versions m
                ON d.metadata_version=m.version WHERE d.dataset_id=?""",
                (key,),
            ).fetchone()
            products = [
                dict(row)
                for row in db.execute(
                    """SELECT p.*,i.status AS integrity_status FROM products p
                    LEFT JOIN integrity i ON i.snapshot_id=p.snapshot_id WHERE dataset_id=?
                    ORDER BY (p.snapshot_id=?) DESC,(kind='source') DESC,p.created_at DESC,p.snapshot_id""",
                    (key, record["current_snapshot_id"]),
                )
            ]
            checks = [
                dict(row)
                for row in db.execute(
                    """SELECT * FROM checks WHERE dataset_id=?
                ORDER BY id DESC LIMIT 5""",
                    (key,),
                )
            ]
            successful = db.execute(
                """SELECT * FROM checks WHERE dataset_id=?
                AND provider_contact=1 AND status='refresh_success' ORDER BY id DESC LIMIT 1""",
                (key,),
            ).fetchone()
        document = json.loads(record["document"])
        return {
            **document,
            "metadata_version": record["metadata_version"],
            "current_snapshot_id": record["current_snapshot_id"],
            "registry_updated_at": record["updated_at"],
            "snapshots": products,
            "availability": {
                "catalogued": True,
                "materialised": bool(products),
                "queryable": any(p["integrity_status"] == "verified" for p in products),
                "current_integrity_status": products[0]["integrity_status"] if products else None,
            },
            "freshness": {
                "recent_checks": checks,
                "provider_check_performed": successful is not None,
                "last_successful_provider_check": dict(successful) if successful else None,
            },
        }

    def search(self, query="", provider=None, limit=20, offset=0):
        if provider is not None and provider not in (*PROVIDERS, "local"):
            raise ValueError("Unknown provider")
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000 or len(query) > 512:
            raise ValueError("Invalid search bounds")
        terms = re.findall(r"\w+", query, re.UNICODE)[:20]
        params = []
        where = []
        join = ""
        if terms:
            join = "JOIN dataset_search s ON s.dataset_id=d.dataset_id"
            where.append("dataset_search MATCH ?")
            params.append(" AND ".join(f'"{term}"*' for term in terms))
        if provider:
            where.append("d.provider=?")
            params.append(provider)
        clause = " WHERE " + " AND ".join(where) if where else ""
        base = f"FROM datasets d {join}{clause}"
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) {base}", params).fetchone()[0]
            records = db.execute(
                f"SELECT d.*, (SELECT document FROM metadata_versions WHERE version=d.metadata_version) "
                f"AS metadata_document {base} ORDER BY d.dataset_id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            items = []
            for record in records:
                item = dict(record)
                metadata = json.loads(item.pop("metadata_document"))
                item["references"] = metadata.get("references", [])
                item["harvest_snapshot_id"] = metadata.get("harvest_snapshot_id")
                item["materialised"] = bool(
                    db.execute(
                        "SELECT 1 FROM products WHERE dataset_id=? LIMIT 1", (item["dataset_id"],)
                    ).fetchone()
                )
                items.append(item)
        end = offset + len(items)
        return {
            "items": items,
            "total": total,
            "truncated": end < total,
            "next_offset": end if end < total else None,
            "coverage": "Curated, materialised and explicitly harvested metadata; see each "
            "harvest snapshot's coverage report for provider scope and completeness",
        }
