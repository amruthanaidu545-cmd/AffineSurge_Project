"""Retrieval-time staleness of generated test cases.

For every node a generation was built from we compare the SNAPSHOT (hash + text frozen at
generation time) with the same *logical* node in the target version (default: latest):

  current         content hash identical
  stale_minor     text changed but no number / unit / error code / comparison symbol / modal-or-negation
                  word changed  (wording, typo, clarification)
  stale_critical  one of those changed (threshold, timing, code list, shall->should, ...)
  source_removed  the logical node no longer exists in the target version

A test case takes the worst status among the nodes it cites. We also report `upstream_changes`:
sections that the source section explicitly references ("see 4.3", "Section 4.2") and that changed
since the snapshot. These never change the status, they are a "review suggested" signal.

HONEST LIMITS: this is lexical, not semantic. It misses meaning changes that keep the same numbers and
modal words (reordered conditions, swapped "systolic"/"diastolic", a changed noun), and it cannot see
contradictions BETWEEN sections (e.g. 4.1 still says "2 seconds" while 4.2 now says 1.5).
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import NodeVersion, Version
from .diffing import classify_change, diff_summary

SEVERITY = {"current": 0, "stale_minor": 1, "stale_critical": 2, "source_removed": 3}
LIMITS = ("Staleness is a lexical heuristic: numbers/units, error codes, comparison symbols and modal or "
          "negation words changing => stale_critical; any other text change => stale_minor. It does not "
          "understand meaning and cannot detect contradictions between sections.")


def _worst(statuses):
    return max(statuses, key=lambda s: SEVERITY[s], default="current")


def _target_version(session: Session, doc_id: int, version_number: int | None) -> Version | None:
    q = select(Version).where(Version.document_id == doc_id)
    q = q.where(Version.number == version_number) if version_number else q.order_by(Version.number.desc())
    return session.scalar(q)


def _find(session, version_id, logical_id):
    return session.scalar(select(NodeVersion).where(NodeVersion.version_id == version_id,
                                                    NodeVersion.logical_node_id == logical_id))


def evaluate(session: Session, gen: dict, against_version: int | None = None) -> dict:
    snap = gen["snapshot"]
    if not snap:
        return {"overall": "current", "per_node": [], "limits": LIMITS}
    target = _target_version(session, snap[0]["document_id"], against_version)
    per_node = {}
    for s in snap:
        entry = {"node_version_id": s["node_version_id"], "logical_node_id": s["logical_node_id"],
                 "section": f"{s['number']} {s['heading']}".strip(), "snapshot_version": s["version_number"],
                 "snapshot_hash": s["content_hash"], "current_hash": None, "current_node_id": None,
                 "status": "source_removed", "diff": None, "upstream_changes": []}
        cur = _find(session, target.id, s["logical_node_id"]) if target else None
        if cur is not None:
            entry.update(current_hash=cur.content_hash, current_node_id=cur.id,
                         current_heading=f"{cur.number or ''} {cur.heading}".strip())
            if cur.content_hash == s["content_hash"]:
                entry["status"] = "current"
            else:
                d = diff_summary(s["text"], cur.body)
                entry["diff"] = d
                entry["status"] = "stale_critical" if d["classification"] == "critical" else "stale_minor"
            for ref in s.get("references", []):  # upstream (cross-reference) changes
                old_ref = session.scalar(select(NodeVersion).where(NodeVersion.version_id == s["version_id"],
                                                                   NodeVersion.number == ref))
                new_ref = _find(session, target.id, old_ref.logical_node_id) if old_ref else None
                if old_ref and (new_ref is None or new_ref.content_hash != old_ref.content_hash):
                    entry["upstream_changes"].append({
                        "referenced_section": f"{ref} {old_ref.heading}",
                        "status": "removed" if new_ref is None else classify_change(old_ref.body, new_ref.body),
                        "message": "referenced section changed since this test case was generated; review suggested"})
        per_node[s["node_version_id"]] = entry

    test_cases = []
    for i, tc in enumerate(gen["test_cases"]):
        cited = [per_node[n] for n in tc["source_node_ids"] if n in per_node] or list(per_node.values())
        status = _worst(e["status"] for e in cited)
        reasons = []
        for e in cited:
            if e["status"] == "current":
                continue
            r = f"{e['section']}: {e['status']}"
            if e["diff"]:
                r += f" (critical changes: +{e['diff']['critical_added']} -{e['diff']['critical_removed']})" \
                    if e["status"] == "stale_critical" else f" (wording: +{e['diff']['added']} -{e['diff']['removed']})"
            reasons.append(r)
        test_cases.append({**tc, "index": i, "staleness": {
            "status": status, "reasons": reasons,
            "review_suggested": status != "current" or any(e["upstream_changes"] for e in cited)}})
    return {"overall": _worst(e["status"] for e in per_node.values()),
            "checked_against_version": target.number if target else None,
            "per_node": list(per_node.values()), "test_cases": test_cases, "limits": LIMITS}
