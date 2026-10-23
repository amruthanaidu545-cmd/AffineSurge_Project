# Approach document

## 1. Parsing approach and why
`ct200_manual*.pdf` has a real text layer, so I used **PyMuPDF positioned/styled text extraction**, not OCR: OCR would
add error (O/0, 1/l) to text that is already exact, which matters when a threshold like "299 mmHg" is the product.
OCR is kept as a **fallback**: a page with no text layer goes through PyMuPDF's Tesseract path; if Tesseract is missing
the page is reported as `unsupported_page` (an *error*) rather than silently skipped. That OCR path is untested here
(no scanned input exists).
Tables use PyMuPDF's table finder for *geometry only*; the cell text is re-read from the page, because the finder's own
text was damaged ("1 .5 seconds", dropped commas) — visible when I compared its output to the PDF dump.
No figures/images exist in either PDF; an image block produces an `unsupported_image` warning instead of being ignored.

## 2. Hierarchy reconstruction
* **Heading = numbered + bold + at left margin.** Font size is *not* a criterion (see 3.2 below); it is only used afterwards to
  *report* deviations.
* **Level = depth of the section number; parent = longest existing numeric prefix** (not "previous heading with lower level").
* Document order is preserved (`ordinal`); children are returned in that order, never re-sorted by number.
* Body is built as typed blocks: `paragraph` / `list` / `table`; `body` is their canonical text; **`content_hash` = SHA-256 of
  the whitespace-collapsed NFKC-normalized body** (own content only, not descendants).
* **Node identity inside a version** = DB id (never the heading), plus a `path` of numbers (`4/4.2`).

## 3. Irregularities found, and how each is handled
| # | Irregularity (how I found it) | Handling |
|---|---|---|
| 1 | **Duplicate heading** "Error Codes" at 4.2 and 7.1 (reading the heading list) | distinct nodes/paths/parents; the version matcher is parent-constrained so they can't cross-match (tests: parser + versioning) |
| 2 | **Level skip** 2.1.1.1 directly under 2.1 (no 2.1.1) | prefix-parent ⇒ child of 2.1, `level=4`, `level_skip` warning surfaced in `/versions/{id}/report` |
| 3 | **Out of order** 3.4 printed before 3.3 | both children of 3 in document order; `out_of_order` warning |
| 4 | **3.2 heading is body-size (11 pt) bold**, other level-2 headings are 12.9 pt (font dump) | a size-based parser would merge 3.2 into 3.1's body *silently*. The numbered+bold+margin rule keeps it; `heading_style_deviation` warning |
| 5 | **Numbered list "1. Normal … 5. Hypertensive Crisis"** inside 3.3 looks like top-level headings | list items are not bold and are indented ⇒ list block; item 5's wrapped line (deeper indent) is joined to item 5 |
| 6 | **Page-break splits**: 3.1's paragraph continues on the next page; headings stranded at page bottoms (2., 4.) with body on the next page | paragraphs join across pages with a space; body is attached to the most recent heading regardless of page |
| 7 | **Headings glued to neighbours in plain-text extraction** ("Specifications2.1 General…", "…attention4. Alarms") | caused by line/page boundaries in naive `get_text`; span-level extraction keeps them as separate lines |
| 8 | **Ligatures / non-breaking hyphen** (ﬁ ﬂ ﬀ, U+2011 in "CT‑200") | NFKC + U+2011→"-" so search and hashes are stable |
| 9 | **Tables with wrapped cells** (4.2) | cell geometry + clipped text; rendered as pipe rows |
| 10 | **Cross-references** ("see 2.1, 4.3", "Section 4.2") look like headings mid-text | never headings (not at line start/bold); parsed into `references` and used for upstream-change signals |
| 11 | Title wraps over 3 lines | leading ≥20 pt lines joined as the document title (root node) |

## 4. What failed, how I found it, what I changed (honest account)
* The parser was designed *after* dumping every line with font size / bold / x / y, so the big traps (3.2 style, glued
  headings, list vs heading) were seen before writing logic. The first parser version passed its tests; I did **not**
  have a long fail/fix cycle there, and I would rather say so than invent one. (Extend this paragraph with your own
  experience if you iterate further.)
* Real failures I did hit and fixed:
  1. Table finder text was corrupted ⇒ switched to geometry-only + re-read text (found by comparing against the PDF dump).
  2. Versioning: inserting nodes before matching violated the `logical_node_id` FK ⇒ the column is nullable during ingest and
     always set before commit (found by the first test run).
  3. Staleness verdicts were nested away from the test cases ⇒ now inlined per test case (found by the e2e test asserting
     what a consumer would read).
  4. A test helper used a 1-character search that the API (correctly) rejects ⇒ helper walks the browse API instead.
* **Validation** (`parser.validate`): token-multiset equality between raw PDF text and the extracted tree. Lost or duplicated
  content becomes an `error`, and `/ingest` returns 422 and persists nothing unless `allow_errors` is set — a clean-looking
  but wrong tree is treated as worse than a refusal.

## 5. Data model
`documents` → `versions` (immutable, source sha256, warnings) → `node_versions` (heading, level, ordinal, path, body, blocks,
content_hash, references, change metadata, parent) ← `logical_nodes` (stable identity across versions);
`selections` → `selection_items(node_version_id)`.
**Version pinning**: a selection item points to a `node_version_id`, which *is* a (logical node, version) pair, and versions are
never mutated, so a selection resolves to the exact original text forever. Generations (JSON documents) store a full
**snapshot** (node_version_id, logical_node_id, hash, text, references) so they survive re-versioning.

## 6. Version matching
Top-down, parent-constrained passes: (1) same number+heading; (2) same heading (unique); (3) same number + fuzzy heading ≥80;
(4) fuzzy heading ≥88 and body similarity ≥0.5; then a global pass for identical non-empty body hash (moved sections).
Unmatched ⇒ `new` (or `removed` in diffs). `modified` ⇔ matched and hash differs.
*Why:* path-only breaks on renumbering; hash-only can't see edits; global fuzzy matching would cross-match the duplicate
"Error Codes". Constraining by parent first removes that class of error.
**Where it breaks:** split/merged sections (matched at best to one half); a heading rename *and* body rewrite at once
(becomes new+removed); a renumbered duplicate-heading pair under the *same* parent (flagged `ambiguous_match`, not
force-matched); a section moved under a different parent *and* edited (the global pass needs an identical hash); a parent
that fails to match drags its subtree into "new" (only the hash pass can rescue leaves).
Observed v1→v2: exactly 4 modified + 1 new, no false positives; 3.1 correctly stays unchanged even though its heading/body
land differently across the page break in v2.

## 7. LLM design
* **Prompt** (`qa-v1`, versioned and stored with every generation): QA-engineer role; only use supplied facts; exact
  values/codes in expected results; 3–5 cases; must cite node ids from `<<<NODE id=…>>>` delimiters; section text is *data,
  not instructions* (prompt-injection hygiene); JSON only, schema included.
* **Validation**: tolerant extraction (strips fences / trailing chatter) → strict Pydantic (3..5 cases, non-empty steps,
  enum priority) → citation check (every `source_node_ids` ⊆ selection ids; hallucinated ids are rejected).
* **Retry**: up to `LLM_MAX_RETRIES` repair attempts that feed the exact validation error back. **Transport** failures
  (429/5xx/timeouts) get short backoff inside the client, are *not* "repaired", and surface as 503.
* **After exhausting retries**: store a `failed` record (all raw attempts kept), return 502/503, store **no** test cases.
  Reason: a partially valid QA suite looks complete and gets trusted; silently truncating 6 → 5 cases hides model drift.
  Failed records don't block retrying the same selection.
* **Duplicate submission policy**: fingerprint = selection id + pinned (node, hash) pairs + prompt version + model. Same
  fingerprint ⇒ return the existing OK generation (`reused: true`, no LLM call, deterministic and free). `regenerate=true`
  ⇒ new generation; the old one is kept and marked `superseded_by`. Reason: LLM output is non-deterministic; silently producing
  a second, different suite for identical input would fork the traceability record.

## 8. Staleness
Computed **at retrieval time** (never stored), so it is always relative to the current/target version: snapshot hash vs the
same *logical* node in the latest version → `current` / `stale_minor` / `stale_critical` / `source_removed`. A test case takes
the worst status of the nodes it cites. Critical = numbers (+units), error codes, `< > ≥ ≤ ±`, or a modal/negation word
(shall/should/not/cannot/…) changed; everything else is minor. Diff tokens are returned so a human sees *why*
(e.g. `critical_removed: [300, 15%]`, `critical_added: [250, 10%]`). `upstream_changes` flags changed sections that a source
section explicitly references (7.1 → 4.2) as "review suggested" without changing the status.
**A one-word wording change is *not* treated like a changed pressure threshold** — deliberate, because
every-change-is-stale causes alert fatigue. **Limits:** it is lexical. It misses same-token semantic changes (reordered
conditions, swapped systolic/diastolic — see `test_known_blind_spot_reordered_condition_is_missed`), and it is noisy in places
(the new E6 row contributes `not` from "does not affect"). It cannot see **contradictions between sections**: in v2, 4.2 says
E3 deflates within 1.5 s but 4.1 still says "within 2 seconds"; a test case built only from 4.1 stays `current` although the
document now contradicts itself. Catching that needs semantic/entity-level checking.

## 9. Stack deviations
* **JSON file store instead of MongoDB.** Generations are self-contained documents queried only by id / selection / logical
  node; atomic tmp+rename writes, a lock, and no infrastructure for a reviewer. Queries scan the directory (O(n)) — fine here,
  wrong at scale. The small store interface is the swap point for MongoDB (indexes on `selection_id` and
  `snapshot.logical_node_id`).
* No other deviations (FastAPI, Pydantic, SQLAlchemy 2, SQLite, pytest).

## 10. With more time
Mongo implementation + indexes; embeddings/NLI-based semantic staleness and cross-section contradiction checks; OCR path tested
on a scanned fixture; a golden-file comparison of the extracted tree against a hand-labelled JSON; Alembic migrations; async LLM
calls with a job queue (generation currently blocks the request, up to ~3 × 45 s); a per-test-case lifecycle
(acknowledge/re-verify) instead of a recomputed status; property-based tests for the matcher.

## Decision log
1. **Most likely to silently give wrong results:** the staleness classifier. Parsing errors are loud (validation → 422) and LLM
   errors are loud (502), but a changed meaning with unchanged numbers/modal words is reported `stale_minor` or `current` and
   nobody is told. Catch: a labelled set of real v1→v2 edits with expected severity run in CI (I only have the
   `known_blind_spot` test), plus sampling "current" verdicts for human review.
2. **Simplicity over correctness:** lexical staleness instead of semantic comparison; JSON-file store with directory scans;
   synchronous LLM calls; a hash over whitespace-collapsed text (a paragraph split/merge is invisible). First to break in
   production: directory-scan retrieval and blocking generation under concurrent users, then the lexical heuristic's false
   confidence on subtler documents.
3. **Not handled:** (a) a scanned/rotated v2 PDF — the parser reports `unsupported_page` (error ⇒ 422) rather than guessing, but
   the OCR path itself is untested; (b) a section split in two in a new version — the matcher pairs it with at most one half;
   (c) an LLM reply that is schema-valid but factually wrong (invents a plausible number) — nothing detects it; the prompt and
   citation check only reduce the chance; (d) the `openai_compat` client was never run against a live provider, so its
   behaviour under real rate limits is unverified.
