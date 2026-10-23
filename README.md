# CT-200 Manual → Structured, Versioned Tree + QA Test-Case Traceability API

FastAPI + Pydantic + SQLAlchemy/SQLite for the tree, versions and selections; a JSON document store for LLM output
(justification in `APPROACH.md`). Parser is PDF-text-layer based (PyMuPDF) with an OCR fallback path.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then export the vars (or prefix commands); see table below
```
| Env var | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` (offline, deterministic) or `openai_compat` (Groq / OpenRouter / Gemini OpenAI-compat endpoint) |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | – | required for `openai_compat`, e.g. `https://api.groq.com/openai/v1` |
| `LLM_JSON_MODE` | `1` | send `response_format=json_object` (set `0` if the provider rejects it) |
| `LLM_MAX_RETRIES` | `2` | validation-repair retries after the first attempt |
| `DATABASE_URL` | `sqlite:///./ct200.db` | SQLAlchemy URL |
| `GENERATION_STORE_DIR` | `./data/generations` | JSON store directory |
| `DATA_DIR` | `./data` | the only directory `/ingest` may read PDFs from |

PDFs: `data/ct200_manual_v1.pdf` (the original `ct200_manual.pdf`) and `data/ct200_manual_v2.pdf`.

## Run
```bash
uvicorn app.main:create_app --factory --port 8000     # docs at http://127.0.0.1:8000/docs
```

## Test
```bash
python -m pytest -q          # 31 tests (~30 s: PDFs are re-parsed per test)
```
`tests/test_parser.py` has one explicit test per PDF irregularity (duplicate "Error Codes", level skip 2.1.1.1,
out-of-order 3.4/3.3, body-size heading 3.2, list-vs-heading, page-break paragraph, tables, ligatures, no content loss).

## The v1 → v2 re-ingestion flow
```bash
# 1. ingest v1  (creates document + version 1)
curl -s -X POST localhost:8000/ingest -H 'content-type: application/json' \
     -d '{"file_path":"ct200_manual_v1.pdf","document_name":"CT-200 Manual"}'
# 2. browse, pick node ids (ids are per-version node ids)
curl -s localhost:8000/sections ; curl -s localhost:8000/nodes/7
# 3. version-pinned selection + generation
curl -s -X POST localhost:8000/selections -H 'content-type: application/json' \
     -d '{"name":"battery+inflation+errors","node_ids":[7,11,16]}'
curl -s -X POST localhost:8000/selections/1/generate
# 4. ingest v2 -> NEW version of the SAME document (v1 untouched); same logical nodes are re-used
curl -s -X POST localhost:8000/ingest -H 'content-type: application/json' \
     -d '{"file_path":"ct200_manual_v2.pdf","document_name":"CT-200 Manual"}'
curl -s localhost:8000/versions/1/diff/2          # added / removed / modified (+ diff summaries)
curl -s localhost:8000/nodes/16/changes           # has THIS node changed? + diff to latest
# 5. staleness is visible at retrieval (by selection id or node id)
curl -s localhost:8000/selections/1/generations
curl -s localhost:8000/nodes/7/generations
```
Or run everything automatically against a running server: `python scripts/demo_flow.py` (use `LLM_PROVIDER=mock`
for an offline run). Expected: v2 ⇒ 24 unchanged, 4 modified (2.1.1.1, 3.2, 4.2, 4.3), 1 new (5.3); the generation
becomes `stale_critical` for 2.1.1.1 / 3.2 / 4.2 and stays `current` for 5.1.

## Endpoints
| Method & path | Purpose |
|---|---|
| `POST /ingest` | parse + persist a PDF as the next version (identical bytes ⇒ no-op; validation errors ⇒ 422, nothing stored unless `allow_errors`) |
| `GET /documents`, `GET /versions/{id}/report` | versions, parser warnings (level skips, out-of-order, style deviations) |
| `GET /sections?version=&document_id=` | top-level sections (default latest) |
| `GET /nodes/{id}` | children, full text, blocks (paragraph/list/table), content hash, breadcrumb, match metadata |
| `GET /search?q=&scope=headings\|text\|all&version=` | search |
| `GET /nodes/{id}/changes` | changed across versions? + diff summary + history |
| `GET /versions/{a}/diff/{b}` | added / removed / modified between two versions |
| `POST /selections`, `GET /selections/{id}` | named, version-pinned selection (resolves to original text forever) |
| `POST /selections/{id}/generate` `{regenerate?}` | 3–5 validated test cases |
| `GET /selections/{id}/generations`, `GET /nodes/{id}/generations`, `GET /generations/{id}` | retrieval **with staleness** (`?against_version=` to compare to a specific version) |

## Not tested / honest status
The `openai_compat` client has been exercised only through unit-level logic, **not against a live provider** (no key in the
build environment); all LLM-behaviour tests use the scripted `MockClient`. See `APPROACH.md` → decision log.
