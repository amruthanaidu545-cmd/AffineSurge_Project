"""Persist a parsed document as a new version and match nodes to the previous version.

Matching is top-down and parent-constrained: a node can only match a child of its
parent's match. Within one parent's children, passes run in decreasing strictness:
  1 exact   same number AND same heading
  2 heading same heading (unique among remaining candidates)  -> survives renumbering
  3 number  same number, heading fuzzy >= 80                  -> survives retitling
  4 fuzzy   heading fuzzy >= 88 AND body similarity >= 0.5
A final global pass pairs still-unmatched nodes with identical non-empty body hash
(moved/reparented sections). Anything else is `new` (and old leftovers are `removed`).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from rapidfuzz import fuzz
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Document, LogicalNode, NodeVersion, Version
from .parser import ParsedDocument, parse_pdf


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _match_group(new_nodes: list[NodeVersion], old_nodes: list[NodeVersion], pairs: dict, warnings: list):
    """pairs: new NodeVersion.id -> (old NodeVersion, method, score). Mutated."""
    free_old = list(old_nodes)
    free_new = list(new_nodes)

    def take(n, o, method, score):
        pairs[n.id] = (o, method, score)
        free_old.remove(o)
        free_new.remove(n)

    for n in list(free_new):  # pass 1
        for o in free_old:
            if n.number == o.number and _norm(n.heading) == _norm(o.heading):
                take(n, o, "number+heading", 1.0)
                break
    for n in list(free_new):  # pass 2
        cands = [o for o in free_old if _norm(o.heading) == _norm(n.heading)]
        if len(cands) == 1:
            take(n, cands[0], "heading", 0.95)
        elif len(cands) > 1:
            warnings.append({"code": "ambiguous_match", "severity": "warning", "number": n.number,
                             "message": f"{n.number} '{n.heading}' matches {len(cands)} siblings by heading; not force-matched"})
    for n in list(free_new):  # pass 3
        for o in free_old:
            r = fuzz.ratio(_norm(n.heading), _norm(o.heading))
            if n.number == o.number and r >= 80:
                take(n, o, "number+fuzzy_heading", r / 100)
                break
    for n in list(free_new):  # pass 4
        best, score = None, 0.0
        for o in free_old:
            h = fuzz.ratio(_norm(n.heading), _norm(o.heading))
            b = fuzz.ratio(n.body, o.body) / 100 if (n.body or o.body) else 0.0
            if h >= 88 and b >= 0.5 and h > score:
                best, score = o, h
        if best:
            take(n, best, "fuzzy_heading+body", score / 100)


def match_versions(new_nodes: list[NodeVersion], old_nodes: list[NodeVersion], warnings: list) -> dict:
    old_by_parent: dict = {}
    for o in old_nodes:
        old_by_parent.setdefault(o.parent_id, []).append(o)
    new_by_parent: dict = {}
    for n in new_nodes:
        new_by_parent.setdefault(n.parent_id, []).append(n)
    pairs: dict = {}
    new_root = next(n for n in new_nodes if n.parent_id is None)
    old_root = next(o for o in old_nodes if o.parent_id is None)
    pairs[new_root.id] = (old_root, "root", 1.0)

    def walk(new_parent: NodeVersion, old_parent: NodeVersion):
        group_new = new_by_parent.get(new_parent.id, [])
        _match_group(group_new, old_by_parent.get(old_parent.id, []), pairs, warnings)
        for n in group_new:
            if n.id in pairs:
                walk(n, pairs[n.id][0])

    walk(new_root, old_root)
    matched_old = {o.id for o, _, _ in pairs.values()}
    free_old = [o for o in old_nodes if o.id not in matched_old and o.body]
    for n in new_nodes:  # global pass: moved sections with identical content
        if n.id in pairs or not n.body:
            continue
        cands = [o for o in free_old if o.content_hash == n.content_hash]
        if len(cands) == 1:
            pairs[n.id] = (cands[0], "content_hash(moved)", 0.9)
            free_old.remove(cands[0])
    return pairs


def ingest_parsed(session: Session, parsed: ParsedDocument, document_name: str,
                  source_file: str, source_sha: str) -> tuple[Version, bool]:
    """Returns (version, created). Re-ingesting byte-identical input is a no-op."""
    doc = session.scalar(select(Document).where(Document.name == document_name))
    if doc is None:
        doc = Document(name=document_name)
        session.add(doc)
        session.flush()
    latest = session.scalar(select(Version).where(Version.document_id == doc.id)
                            .order_by(Version.number.desc()))
    if latest and latest.source_sha256 == source_sha:
        return latest, False
    ver = Version(document_id=doc.id, number=(latest.number + 1 if latest else 1),
                  source_file=source_file, source_sha256=source_sha, title=parsed.title,
                  warnings=list(parsed.warnings))
    session.add(ver)
    session.flush()

    created: list[NodeVersion] = []
    idx2nv: dict[int, NodeVersion] = {}
    for pn in parsed.nodes:  # parents always precede children in document order
        nv = NodeVersion(
            logical_node_id=None, version_id=ver.id,
            parent_id=idx2nv[pn.parent].id if pn.parent is not None else None,
            number=pn.number, heading=pn.heading, level=pn.level, ordinal=pn.ordinal, path=pn.path,
            body=pn.body, blocks=pn.blocks, content_hash=pn.content_hash, references=pn.references)
        session.add(nv)
        session.flush()
        idx2nv[pn.index] = nv
        created.append(nv)

    pairs: dict = {}
    if latest:
        old_nodes = list(session.scalars(select(NodeVersion).where(NodeVersion.version_id == latest.id)))
        pairs = match_versions(created, old_nodes, ver.warnings)
    for nv in created:
        if nv.id in pairs:
            old, method, score = pairs[nv.id]
            nv.logical_node_id = old.logical_node_id
            nv.prev_node_version_id, nv.match_method, nv.match_score = old.id, method, score
            nv.change_status = "unchanged" if nv.content_hash == old.content_hash else "modified"
            nv.heading_changed = _norm(nv.heading) != _norm(old.heading)
            nv.number_changed = nv.number != old.number
        else:
            ln = LogicalNode(document_id=doc.id, first_version_id=ver.id)
            session.add(ln)
            session.flush()
            nv.logical_node_id = ln.id
            nv.change_status = "new"
    ver.warnings = list(ver.warnings)  # re-assign so JSON change is persisted
    session.commit()
    return ver, True


def ingest_file(session: Session, path: str, document_name: str) -> tuple[Version, bool]:
    raw = Path(path).read_bytes()
    parsed = parse_pdf(path)
    return ingest_parsed(session, parsed, document_name, Path(path).name,
                         hashlib.sha256(raw).hexdigest())


def latest_version(session: Session, document_id: int) -> Version | None:
    return session.scalar(select(Version).where(Version.document_id == document_id)
                          .order_by(Version.number.desc()))
