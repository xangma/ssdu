"""Portable, blind review forms for frozen discovery result pools.

Export does not mark any pair reviewed. The HTML runs entirely offline and emits
only complete judgments explicitly entered by a named human reviewer.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any


class ReviewExportError(ValueError):
    """A review pack cannot be exported without preserving its binding."""


def _json(value: Any) -> str:
    try:
        return (
            json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        )
    except (TypeError, ValueError) as exc:
        raise ReviewExportError("Review data must contain finite JSON values") from exc


def _binding(queue: dict) -> dict:
    binding = queue.get("binding")
    if not isinstance(binding, dict) or not isinstance(binding.get("benchmark_id"), str):
        raise ReviewExportError("Queue requires a benchmark binding")
    digest = queue.get("queue_sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ReviewExportError("Queue requires a SHA-256 binding")
    return {"queue_sha256": digest, "benchmark_id": binding["benchmark_id"]}


def _blind_data(queue: dict) -> dict:
    """Whitelist review context; previous grades and method ranks stay out of the form."""
    questions = queue.get("questions")
    rows = queue.get("rows")
    if not isinstance(questions, list) or not isinstance(rows, list):
        raise ReviewExportError("Queue questions and rows must be lists")
    return {
        **_binding(queue),
        "questions": [
            {
                key: question.get(key)
                for key in (
                    "id",
                    "text",
                    "constraints",
                    "references",
                    "identity_levels",
                    "scenario_basis",
                    "adaptation_notes",
                )
            }
            for question in questions
        ],
        "rows": [
            {
                "row_id": row.get("id"),
                "question_id": row.get("question_id"),
                "dataset_id": row.get("dataset_id"),
                "dataset": row.get("dataset"),
                "references": [
                    *(row.get("question_references") or []),
                    *(row.get("dataset_references") or []),
                ],
                "constraint_checks": row.get("coverage_check"),
            }
            for row in rows
        ],
    }


def _csv(data: dict) -> str:
    stream = io.StringIO(newline="")
    fields = [
        "row_id",
        "question_id",
        "question",
        "dataset_id",
        "dataset_title",
        "constraints_json",
        "dataset_json",
        "references_json",
        "relevance",
        "identity",
        "suitability",
        "reason",
        "reviewer_name",
        "reviewed_at",
    ]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    questions = {question["id"]: question for question in data["questions"]}
    for row in data["rows"]:
        question = questions.get(row["question_id"], {})
        dataset = row.get("dataset") or {}
        refs = _references(question, row)
        record = {
            "row_id": row["row_id"],
            "question_id": row["question_id"],
            "question": question.get("text", ""),
            "dataset_id": row["dataset_id"],
            "dataset_title": dataset.get("title", row["dataset_id"]),
            "constraints_json": json.dumps(question.get("constraints", {}), ensure_ascii=False),
            "dataset_json": json.dumps(dataset, ensure_ascii=False),
            "references_json": json.dumps(refs, ensure_ascii=False),
        }
        # Prefix spreadsheet formulas in CSV cells; queue.json retains exact source text.
        writer.writerow(
            {
                key: "'" + value
                if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r"}
                else value
                for key, value in record.items()
            }
        )
    return stream.getvalue()


def _references(question: dict, row: dict) -> list[dict]:
    result = {}
    for ref in [*(question.get("references") or []), *(row.get("references") or [])]:
        if isinstance(ref, dict) and ref.get("id"):
            result[ref["id"]] = {key: ref.get(key) for key in ("id", "title", "url")}
    return list(result.values())


def export_review_pack(queue: dict, output_dir: str | Path) -> dict:
    """Write a deterministic review pack, refusing a conflicting existing pack.

    Re-exporting the same queue is safe. Existing non-pack content and symlinks
    are refused, so this command cannot silently replace unrelated files.
    """
    data = _blind_data(queue)
    template = {**_binding(queue), "judgments": []}
    embedded = _json(data)
    for character, replacement in (
        ("&", "\\u0026"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        embedded = embedded.replace(character, replacement)
    files = {
        "queue.json": _json(queue),
        "rows.csv": _csv(data),
        "judgments.template.json": _json(template),
        "review.html": _HTML.replace("__REVIEW_DATA__", embedded),
    }
    output = Path(output_dir)
    if output.is_symlink():
        raise ReviewExportError("Review pack directory must not be a symlink")
    if output.exists():
        if not output.is_dir():
            raise ReviewExportError("Review pack output must be a directory")
        if any(path.is_symlink() for path in output.iterdir()):
            raise ReviewExportError("Review pack must not contain symlinks")
        if any(output.iterdir()):
            try:
                previous = json.loads((output / "queue.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReviewExportError("Existing output is not a review pack") from exc
            if _binding(previous) != _binding(queue) or _json(previous) != _json(queue):
                raise ReviewExportError("Existing output belongs to a different review queue")
            if {path.name for path in output.iterdir()} - set(files):
                raise ReviewExportError("Review pack directory contains unrelated files")
    output.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, content in files.items():
        payload = content.encode("utf-8")
        (output / name).write_bytes(payload)
        hashes[name] = hashlib.sha256(payload).hexdigest()
    return {
        **_binding(queue),
        "output_dir": str(output.resolve()),
        "question_count": len(data["questions"]),
        "row_count": len(data["rows"]),
        "completed_judgments": 0,
        "files": hashes,
    }


_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>Discovery result review</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#172c3b;background:#f6f8fa}
h1{font-size:1.8rem}h2{font-size:1.3rem}h3{font-size:1.1rem}button,input,select,textarea{font:inherit;padding:.4rem;max-width:100%;box-sizing:border-box}
button{cursor:pointer}label{display:block;margin:.6rem 0}textarea{width:100%;min-height:5rem}article,.controls,details{border:1px solid #c8d2da;background:white;border-radius:.5rem;padding:1rem;margin:1rem 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.7rem}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:.85rem}
.muted{color:#526675}.notice{background:#fff6d8;padding:.7rem}a{color:#145990}summary{cursor:pointer}.status{font-weight:600}.hidden{display:none}
</style></head><body>
<h1>Review discovery results</h1>
<p>Assess each question–dataset pair independently. Search methods and previous grades are hidden. These are candidate judgments from a pooled set; they do not establish complete coverage of all datasets.</p>
<details><summary>Judgment guidance</summary>
<ul><li><strong>Relevance:</strong> 3 = directly answers the research need; 2 = useful; 1 = related; 0 = irrelevant. Judge the question, rather than whether an earlier system selected this result.</li>
<li><strong>Dataset identity:</strong> confirmed = the specific study dataset/release is evidenced; current compatible = a current dataset matches the subject but the historical identity is unproven; family = only the dataset family is identified; unresolved = evidence is insufficient.</li>
<li><strong>Suitability:</strong> eligible = meets all stated analysis constraints; ineligible = a known mismatch; unknown = a required property is unverified. Topic relevance does not prove geography, release, period or access suitability.</li>
<li>Inspect references and metadata, give a short evidence-based reason, and choose unknown/unresolved where needed. Automatic coverage checks are hints, not human judgments. Reference links may require internet access; the form itself makes no network requests.</li>
<li>Only a person who inspected the evidence should enter a reviewer name. Save completed judgments before closing this file. Pending and partial rows are excluded from downloads.</li></ul>
</details>
<div class="controls"><div class="grid">
<label>Your name <input id="reviewer" autocomplete="name" placeholder="Human reviewer name" required></label>
<label>Question <select id="question"></select></label>
<label>Filter datasets <input id="filter" type="search" placeholder="Title or identifier"></label>
<label>Rows <select id="status"><option value="all">All</option><option value="pending">Pending</option><option value="complete">Complete</option></select></label>
</div><button id="save" type="button">Download completed judgments</button>
<label>Resume from a judgment file <input id="resume" type="file" accept="application/json,.json"></label>
<p id="progress" class="status" aria-live="polite"></p><p id="message" aria-live="polite"></p>
<p class="notice">Edits stay in this browser tab until downloaded. Keep the downloaded JSON with its original queue hash.</p></div>
<section id="context"></section><main id="rows"></main>
<script id="review-data" type="application/json">__REVIEW_DATA__</script>
<script>
'use strict';
const data=JSON.parse(document.getElementById('review-data').textContent);
const questions=new Map(data.questions.map(q=>[q.id,q]));
const rowsById=new Map(data.rows.map(r=>[r.row_id,r]));
const drafts=new Map();
const node=(tag,text,className)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(className)n.className=className;return n;};
const byId=id=>document.getElementById(id);
const complete=d=>d && Number.isInteger(d.relevance) && d.relevance>=0 && d.relevance<=3 && ['confirmed','current_compatible','family','unresolved'].includes(d.identity) && ['eligible','ineligible','unknown'].includes(d.suitability) && typeof d.reason==='string' && d.reason.trim().length>0;
function references(q,r){const refs=new Map();for(const ref of [...(q.references||[]),...(r.references||[])])if(ref && ref.id)refs.set(ref.id,{id:ref.id,title:ref.title,url:ref.url});return [...refs.values()];}
function showReferences(parent,refs){const list=node('ul');for(const ref of refs){const li=node('li');let url;try{url=new URL(ref.url);}catch{}if(url && ['https:','http:'].includes(url.protocol)){const a=node('a',ref.title||ref.id);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';li.append(a);}else li.append(node('span',ref.title||ref.id));li.append(node('span',' ['+ref.id+']','muted'));list.append(li);}parent.append(list);}
function showJson(parent,title,value){const box=node('details');box.append(node('summary',title),node('pre',JSON.stringify(value||{},null,2)));parent.append(box);}
function selection(label,key,values,draft,rowId){const wrapper=node('label',label+' ');const select=node('select');select.append(node('option','Choose…'));select.options[0].value='';for(const [value,title] of values){const option=node('option',title);option.value=String(value);select.append(option);}select.value=draft[key]===undefined?'':String(draft[key]);select.addEventListener('change',()=>{draft[key]=select.value===''?undefined:key==='relevance'?Number(select.value):select.value;draft.reviewed_at=new Date().toISOString();drafts.set(rowId,draft);updateProgress();});wrapper.append(select);return wrapper;}
function updateProgress(){const count=[...drafts.values()].filter(complete).length;byId('progress').textContent=count+' complete / '+data.rows.length+' candidate pairs. '+(data.rows.length-count)+' pending.';}
function render(){const selected=byId('question').value;const q=questions.get(selected);const context=byId('context');context.replaceChildren();if(q){context.append(node('h2',q.text),node('p','Question '+q.id,'muted'));showJson(context,'Requested constraints',q.constraints);if(q.scenario_basis || q.adaptation_notes)showJson(context,'Question context',{scenario_basis:q.scenario_basis,adaptation_notes:q.adaptation_notes,identity_levels:q.identity_levels});showReferences(context,q.references||[]);}const root=byId('rows');root.replaceChildren();const term=byId('filter').value.toLowerCase();const status=byId('status').value;let shown=0;for(const row of data.rows){if(row.question_id!==selected)continue;const dataset=row.dataset||{};if(!((dataset.title||'')+' '+row.dataset_id).toLowerCase().includes(term))continue;const draft=drafts.get(row.row_id)||{};if(status==='pending' && complete(draft) || status==='complete' && !complete(draft))continue;const article=node('article');article.append(node('h3',dataset.title||row.dataset_id),node('p',row.dataset_id,'muted'));if(dataset.description)article.append(node('p',dataset.description));showJson(article,'Dataset metadata',dataset);showJson(article,'Automatic coverage checks',row.constraint_checks);showReferences(article,references(q||{},row));const choices=node('div',undefined,'grid');choices.append(selection('Relevance','relevance',[[3,'3 — direct'],[2,'2 — useful'],[1,'1 — related'],[0,'0 — irrelevant']],draft,row.row_id),selection('Dataset identity','identity',[['confirmed','Confirmed study dataset/release'],['current_compatible','Current compatible dataset'],['family','Dataset family only'],['unresolved','Unresolved']],draft,row.row_id),selection('Analysis suitability','suitability',[['eligible','Eligible'],['ineligible','Ineligible'],['unknown','Unknown']],draft,row.row_id));article.append(choices);const reasonLabel=node('label','Evidence and reason');const reason=node('textarea');reason.value=draft.reason||'';reason.addEventListener('input',()=>{draft.reason=reason.value;draft.reviewed_at=new Date().toISOString();drafts.set(row.row_id,draft);updateProgress();});reasonLabel.append(reason);article.append(reasonLabel);article.append(node('p','Pair '+row.row_id,'muted'));root.append(article);shown++;}if(!shown)root.append(node('p','No rows match this filter.'));updateProgress();}
for(const q of data.questions){const option=node('option',q.id+' — '+q.text);option.value=q.id;byId('question').append(option);}
for(const id of ['question','filter','status'])byId(id).addEventListener('input',render);
byId('save').addEventListener('click',()=>{const name=byId('reviewer').value.trim();if(!name){byId('message').textContent='Enter the name of the person who reviewed the evidence.';return;}const judgments=[];for(const row of data.rows){const d=drafts.get(row.row_id);if(!complete(d))continue;judgments.push({row_id:row.row_id,reviewer:{type:'human',name},reviewed_at:d.reviewed_at||new Date().toISOString(),reason:d.reason.trim(),references:references(questions.get(row.question_id)||{},row),relevance:d.relevance,identity:d.identity,suitability:d.suitability});}if(!judgments.length){byId('message').textContent='No complete judgments to download. Pending rows remain unjudged.';return;}const blob=new Blob([JSON.stringify({queue_sha256:data.queue_sha256,benchmark_id:data.benchmark_id,judgments},null,2)+'\n'],{type:'application/json'});const url=URL.createObjectURL(blob);const a=node('a');a.href=url;a.download='discovery-judgments-'+data.queue_sha256.slice(0,12)+'.json';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);byId('message').textContent='Downloaded '+judgments.length+' complete judgments. Import the file through the review CLI to validate it.';});
byId('resume').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;try{if(file.size>10000000)throw Error('Judgment file exceeds 10 MB.');const saved=JSON.parse(await file.text());if(saved.queue_sha256!==data.queue_sha256 || saved.benchmark_id!==data.benchmark_id || !Array.isArray(saved.judgments))throw Error('This file belongs to a different queue.');const pending=new Map();let name;for(const j of saved.judgments){if(!rowsById.has(j.row_id) || pending.has(j.row_id) || !complete(j) || j.reviewer?.type!=='human' || typeof j.reviewer.name!=='string' || !j.reviewer.name.trim() || !Number.isFinite(Date.parse(j.reviewed_at)))throw Error('Malformed, duplicate or unknown judgment.');if(name!==undefined && name!==j.reviewer.name)throw Error('Resume files must contain judgments by one reviewer.');name=j.reviewer.name;pending.set(j.row_id,{relevance:j.relevance,identity:j.identity,suitability:j.suitability,reason:j.reason,reviewed_at:j.reviewed_at});}for(const [id,draft] of pending)drafts.set(id,draft);if(name)byId('reviewer').value=name;byId('message').textContent='Restored '+pending.size+' judgments. The CLI checks provenance on import.';render();}catch(error){byId('message').textContent='Could not resume: '+error.message;}event.target.value='';});
render();
</script></body></html>
"""
