"""FastAPI app: ingest, browse, version diff, selections, generation, staleness-aware retrieval."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pymupdf
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db import Document, NodeVersion, Selection, SelectionItem, Version, make_session_factory
from .diffing import diff_summary
from .generation import run_generation
from .llm import client_from_env
from .parser import parse_pdf
from .staleness import evaluate
from .store import JsonFileStore
from .versioning import ingest_parsed, latest_version

MAX_SELECTION_NODES = 25


class IngestRequest(BaseModel):
    file_path: str = Field(description="PDF path relative to DATA_DIR, e.g. 'ct200_manual_v2.pdf'")
    document_name: str = Field(default="CT-200 Manual", min_length=1, max_length=200)
    allow_errors: bool = Field(default=False, description="persist even if validation found errors")


class SelectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    node_ids: list[int] = Field(min_length=1, max_length=MAX_SELECTION_NODES)


class GenerateRequest(BaseModel):
    regenerate: bool = False


def create_app(database_url: str | None = None, store_dir: str | None = None,
               data_dir: str | None = None, llm=None) -> FastAPI:
    app = FastAPI(title="CT-200 Manual Structuring & QA Traceability API")
    app.state.SessionLocal = make_session_factory(database_url or os.getenv("DATABASE_URL", "sqlite:///./ct200.db"))
    app.state.store = JsonFileStore(store_dir or os.getenv("GENERATION_STORE_DIR", "./data/generations"))
    app.state.data_dir = Path(data_dir or os.getenv("DATA_DIR", "./data")).resolve()
    app.state.llm = llm or client_from_env()
    app.state.max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))

    def get_session(request: Request):
        s = request.app.state.SessionLocal()
        try:
            yield s
        finally:
            s.close()

    # ------------------------------------------------------------ helpers
    def version_or_404(s: Session, document_id: int | None, number: int | None) -> Version:
        if document_id is None:
            doc = s.scalar(select(Document).order_by(Document.id))
            if doc is None:
                raise HTTPException(404, "no documents ingested yet; POST /ingest first")
            document_id = doc.id
        q = select(Version).where(Version.document_id == document_id)
        q = q.where(Version.number == number) if number else q.order_by(Version.number.desc())
        v = s.scalar(q)
        if v is None:
            raise HTTPException(404, "version not found")
        return v

    def node_or_404(s: Session, node_id: int) -> NodeVersion:
        nv = s.get(NodeVersion, node_id)
        if nv is None:
            raise HTTPException(404, f"node {node_id} not found")
        return nv

    def brief(nv: NodeVersion) -> dict:
        return {"id": nv.id, "number": nv.number, "heading": nv.heading, "level": nv.level,
                "content_hash": nv.content_hash, "change_status": nv.change_status}

    def full(s: Session, nv: NodeVersion) -> dict:
        ver = s.get(Version, nv.version_id)
        kids = s.scalars(select(NodeVersion).where(NodeVersion.parent_id == nv.id).order_by(NodeVersion.ordinal))
        crumbs, p = [], nv
        while p.parent_id is not None:
            p = s.get(NodeVersion, p.parent_id)
            if p.number:
                crumbs.append({"id": p.id, "number": p.number, "heading": p.heading})
        return {**brief(nv), "version": ver.number, "version_id": ver.id, "logical_node_id": nv.logical_node_id,
                "parent_id": nv.parent_id, "breadcrumb": list(reversed(crumbs)), "path": nv.path,
                "ordinal": nv.ordinal, "text": nv.body, "blocks": nv.blocks, "references": nv.references,
                "heading_changed": nv.heading_changed, "number_changed": nv.number_changed,
                "match_method": nv.match_method, "match_score": nv.match_score,
                "children": [brief(k) for k in kids]}

    def present(s: Session, rec: dict, against: int | None = None) -> dict:
        if rec["status"] != "ok":
            return rec
        ev = evaluate(s, rec, against)
        cases = ev.pop("test_cases")  # verdict lives on each test case; summary stays under "staleness"
        return {**rec, "test_cases": cases, "staleness": ev}

    # ------------------------------------------------------------ ingest
    @app.post("/ingest", status_code=201)
    def ingest(req: IngestRequest, request: Request, s: Session = Depends(get_session)):
        path = (request.app.state.data_dir / req.file_path).resolve()
        if request.app.state.data_dir not in path.parents or path.suffix.lower() != ".pdf":
            raise HTTPException(400, "file_path must be a .pdf inside DATA_DIR")
        if not path.is_file():
            raise HTTPException(404, f"{req.file_path} not found in DATA_DIR")
        try:
            parsed = parse_pdf(str(path))
        except (ValueError, RuntimeError, pymupdf.FileDataError) as e:
            raise HTTPException(422, f"could not parse PDF: {e}")
        errors = [w for w in parsed.warnings if w["severity"] == "error"]
        if errors and not req.allow_errors:
            return JSONResponse(status_code=422, content={
                "detail": "extraction validation failed; nothing was persisted (use allow_errors to override)",
                "errors": errors})
        ver, created = ingest_parsed(s, parsed, req.document_name, path.name,
                                     hashlib.sha256(path.read_bytes()).hexdigest())
        counts = dict(s.execute(select(NodeVersion.change_status, func.count()).where(
            NodeVersion.version_id == ver.id).group_by(NodeVersion.change_status)).all())
        return {"document_id": ver.document_id, "version": ver.number, "version_id": ver.id, "created": created,
                "node_count": sum(counts.values()), "change_counts": counts, "warnings": ver.warnings,
                "message": None if created else "identical file already ingested; no new version created"}

    @app.get("/documents")
    def documents(s: Session = Depends(get_session)):
        out = []
        for d in s.scalars(select(Document)):
            vs = s.scalars(select(Version).where(Version.document_id == d.id).order_by(Version.number)).all()
            out.append({"id": d.id, "name": d.name, "versions": [
                {"version": v.number, "version_id": v.id, "source_file": v.source_file,
                 "created_at": v.created_at.isoformat(), "warning_count": len(v.warnings)} for v in vs]})
        return out

    @app.get("/versions/{version_id}/report")
    def report(version_id: int, s: Session = Depends(get_session)):
        v = s.get(Version, version_id)
        if v is None:
            raise HTTPException(404, "version not found")
        return {"version": v.number, "title": v.title, "warnings": v.warnings}

    # ------------------------------------------------------------ browse
    @app.get("/sections")
    def sections(version: int | None = None, document_id: int | None = None, s: Session = Depends(get_session)):
        v = version_or_404(s, document_id, version)
        root = s.scalar(select(NodeVersion).where(NodeVersion.version_id == v.id, NodeVersion.parent_id.is_(None)))
        kids = s.scalars(select(NodeVersion).where(NodeVersion.parent_id == root.id).order_by(NodeVersion.ordinal))
        return {"document_id": v.document_id, "version": v.number, "title": v.title,
                "root_node_id": root.id, "sections": [brief(k) for k in kids]}

    @app.get("/nodes/{node_id}")
    def get_node(node_id: int, s: Session = Depends(get_session)):
        return full(s, node_or_404(s, node_id))

    @app.get("/search")
    def search(q: str = Query(min_length=2, max_length=100), scope: str = Query("all", pattern="^(headings|text|all)$"),
               version: int | None = None, document_id: int | None = None, limit: int = Query(50, le=200),
               s: Session = Depends(get_session)):
        v = version_or_404(s, document_id, version)
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds = []
        if scope in ("headings", "all"):
            conds.append(NodeVersion.heading.ilike(like, escape="\\"))
        if scope in ("text", "all"):
            conds.append(NodeVersion.body.ilike(like, escape="\\"))
        rows = s.scalars(select(NodeVersion).where(NodeVersion.version_id == v.id, or_(*conds))
                         .order_by(NodeVersion.ordinal).limit(limit)).all()
        out = []
        for n in rows:
            i = n.body.lower().find(q.lower())
            snippet = n.body[max(0, i - 40): i + 80] if i >= 0 else None
            out.append({**brief(n), "snippet": snippet})
        return {"version": v.number, "query": q, "count": len(out), "results": out}

    @app.get("/nodes/{node_id}/changes")
    def node_changes(node_id: int, s: Session = Depends(get_session)):
        nv = node_or_404(s, node_id)
        rows = s.execute(select(NodeVersion, Version).join(Version, Version.id == NodeVersion.version_id)
                         .where(NodeVersion.logical_node_id == nv.logical_node_id).order_by(Version.number)).all()
        history, prev = [], None
        for n, v in rows:
            history.append({"version": v.number, "node_id": n.id, "number": n.number, "heading": n.heading,
                            "content_hash": n.content_hash, "change_status": n.change_status,
                            "diff_from_previous": diff_summary(prev.body, n.body) if prev and n.change_status == "modified" else None})
            prev = n
        doc_id = s.get(Version, nv.version_id).document_id
        latest = latest_version(s, doc_id)
        cur = next((n for n, v in rows if v.id == latest.id), None)
        this = s.get(Version, nv.version_id)
        if cur is None:
            status, d = "removed_in_latest", None
        elif cur.content_hash == nv.content_hash:
            status, d = "unchanged_in_latest", None
        else:
            status, d = "changed_in_latest", diff_summary(nv.body, cur.body)
        return {"node_id": nv.id, "logical_node_id": nv.logical_node_id, "this_version": this.number,
                "latest_version": latest.number, "status": status, "diff_to_latest": d, "history": history}

    @app.get("/versions/{a}/diff/{b}")
    def version_diff(a: int, b: int, s: Session = Depends(get_session)):
        va, vb = s.get(Version, a), s.get(Version, b)
        if not va or not vb or va.document_id != vb.document_id:
            raise HTTPException(404, "versions not found or belong to different documents")
        na = {n.logical_node_id: n for n in s.scalars(select(NodeVersion).where(NodeVersion.version_id == a))}
        nb = {n.logical_node_id: n for n in s.scalars(select(NodeVersion).where(NodeVersion.version_id == b))}
        lab = lambda n: f"{n.number or ''} {n.heading}".strip()  # noqa: E731
        return {"from": va.number, "to": vb.number,
                "added": [{"node_id": n.id, "section": lab(n)} for k, n in nb.items() if k not in na],
                "removed": [{"node_id": n.id, "section": lab(n)} for k, n in na.items() if k not in nb],
                "modified": [{"node_id": nb[k].id, "from_node_id": n.id, "section": lab(nb[k]),
                              "diff": diff_summary(n.body, nb[k].body)}
                             for k, n in na.items() if k in nb and n.content_hash != nb[k].content_hash],
                "unchanged_count": sum(1 for k, n in na.items() if k in nb and n.content_hash == nb[k].content_hash)}

    # ------------------------------------------------------------ selections
    def selection_out(s: Session, sel: Selection, with_text: bool = False) -> dict:
        items = []
        for it in sel.items:
            nv = s.get(NodeVersion, it.node_version_id)
            ver = s.get(Version, nv.version_id)
            d = {"node_id": nv.id, "version": ver.number, "number": nv.number, "heading": nv.heading,
                 "content_hash": nv.content_hash}
            if with_text:
                d["text"] = nv.body
            items.append(d)
        return {"id": sel.id, "name": sel.name, "created_at": sel.created_at.isoformat(), "items": items}

    @app.post("/selections", status_code=201)
    def create_selection(req: SelectionCreate, s: Session = Depends(get_session)):
        ids = list(dict.fromkeys(req.node_ids))
        found = {n.id: n for n in s.scalars(select(NodeVersion).where(NodeVersion.id.in_(ids)))}
        missing = [i for i in ids if i not in found]
        if missing:
            raise HTTPException(422, f"unknown node ids: {missing}")
        sel = Selection(name=req.name)
        s.add(sel)
        s.flush()
        for i in ids:  # pinned: node_version_id already encodes (logical node, version)
            s.add(SelectionItem(selection_id=sel.id, node_version_id=i))
        s.commit()
        s.refresh(sel)
        return selection_out(s, sel)

    @app.get("/selections/{selection_id}")
    def get_selection(selection_id: int, s: Session = Depends(get_session)):
        sel = s.get(Selection, selection_id)
        if sel is None:
            raise HTTPException(404, "selection not found")
        return selection_out(s, sel, with_text=True)

    # ------------------------------------------------------------ generation
    @app.post("/selections/{selection_id}/generate")
    def generate(selection_id: int, request: Request, req: GenerateRequest = GenerateRequest(),
                 s: Session = Depends(get_session)):
        sel = s.get(Selection, selection_id)
        if sel is None:
            raise HTTPException(404, "selection not found")
        rec, reused = run_generation(s, request.app.state.store, request.app.state.llm, sel,
                                     req.regenerate, request.app.state.max_retries)
        if rec["status"] != "ok":
            code = 503 if rec["error"]["type"] == "transport" else 502
            return JSONResponse(status_code=code, content={
                "detail": "LLM generation failed; no test cases were stored as valid output",
                "error": rec["error"], "generation_id": rec["id"], "attempts": len(rec["attempts"])})
        return {**present(s, rec), "reused": reused}

    # ------------------------------------------------------------ retrieval (staleness visible here)
    @app.get("/generations/{generation_id}")
    def get_generation(generation_id: str, against_version: int | None = None, request: Request = None,
                       s: Session = Depends(get_session)):
        rec = request.app.state.store.get(generation_id)
        if rec is None:
            raise HTTPException(404, "generation not found")
        return present(s, rec, against_version)

    @app.get("/selections/{selection_id}/generations")
    def selection_generations(selection_id: int, request: Request, include_failed: bool = False,
                              against_version: int | None = None, s: Session = Depends(get_session)):
        if s.get(Selection, selection_id) is None:
            raise HTTPException(404, "selection not found")
        recs = request.app.state.store.by_selection(selection_id)
        return [present(s, r, against_version) for r in recs if include_failed or r["status"] == "ok"]

    @app.get("/nodes/{node_id}/generations")
    def node_generations(node_id: int, request: Request, include_failed: bool = False,
                         against_version: int | None = None, s: Session = Depends(get_session)):
        nv = node_or_404(s, node_id)
        recs = request.app.state.store.by_logical_node(nv.logical_node_id)  # logical id => works across versions
        return [present(s, r, against_version) for r in recs if include_failed or r["status"] == "ok"]

    return app

