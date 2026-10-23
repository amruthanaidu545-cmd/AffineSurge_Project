"""End-to-end: v1 ingest -> selection -> generation -> v2 re-ingest -> staleness at retrieval."""
import shutil

import pytest
from fastapi.testclient import TestClient

from app.llm import MockClient
from app.main import create_app


@pytest.fixture()
def env(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    for n in ("ct200_manual_v1.pdf", "ct200_manual_v2.pdf"):
        shutil.copy(f"data/{n}", data / n)
    llm = MockClient(["good"])
    app = create_app("sqlite:///:memory:", str(tmp_path / "gens"), str(data), llm)
    return TestClient(app), llm


def ingest(c, name):
    r = c.post("/ingest", json={"file_path": name})
    assert r.status_code == 201, r.text
    return r.json()


def find(c, version, number):
    """Resolve a section number to a node id by walking the browse API."""
    stack = [x["id"] for x in c.get("/sections", params={"version": version}).json()["sections"]]
    while stack:
        n = c.get(f"/nodes/{stack.pop()}").json()
        if n["number"] == number:
            return n["id"]
        stack += [k["id"] for k in n["children"]]
    raise AssertionError(f"section {number} not found in v{version}")


def test_full_versioning_and_staleness_flow(env):
    c, llm = env
    v1 = ingest(c, "ct200_manual_v1.pdf")
    assert v1["version"] == 1 and v1["node_count"] == 28
    assert any(w["code"] == "level_skip" for w in v1["warnings"])

    ids = [find(c, 1, n) for n in ("2.1.1.1", "3.2", "4.2", "5.1")]
    sel = c.post("/selections", json={"name": "risk sections", "node_ids": ids}).json()
    gen = c.post(f"/selections/{sel['id']}/generate").json()
    assert gen["status"] == "ok" and 3 <= len(gen["test_cases"]) <= 5 and gen["reused"] is False
    assert gen["staleness"]["overall"] == "current"

    # duplicate submission policy: same selection+content -> same generation, no new LLM call
    calls = llm.calls
    again = c.post(f"/selections/{sel['id']}/generate").json()
    assert again["id"] == gen["id"] and again["reused"] is True and llm.calls == calls

    v2 = ingest(c, "ct200_manual_v2.pdf")
    assert v2["version"] == 2 and v2["change_counts"] == {"unchanged": 24, "modified": 4, "new": 1}
    assert c.post("/ingest", json={"file_path": "ct200_manual_v2.pdf"}).json()["created"] is False

    # old selection still resolves to exactly the v1 text
    pinned = c.get(f"/selections/{sel['id']}").json()
    t = next(i for i in pinned["items"] if i["number"] == "2.1.1.1")
    assert t["version"] == 1 and "300 measurement cycles" in t["text"]

    # staleness visible via selection id AND node id
    by_sel = c.get(f"/selections/{sel['id']}/generations").json()[0]
    st = {n["section"]: n["status"] for n in by_sel["staleness"]["per_node"]}
    assert st["2.1.1.1 Battery Life Under Typical Use"] == "stale_critical"
    assert st["3.2 Cuff Inflation Sequence"] == "stale_critical"
    assert st["4.2 Error Codes"] == "stale_critical"
    assert st["5.1 Local Storage"] == "current"
    assert by_sel["staleness"]["checked_against_version"] == 2
    crit = {n["section"]: n["diff"] for n in by_sel["staleness"]["per_node"] if n["diff"]}
    assert "300" in crit["2.1.1.1 Battery Life Under Typical Use"]["critical_removed"]
    by_node = c.get(f"/nodes/{ids[0]}/generations").json()
    assert by_node[0]["id"] == gen["id"] and by_node[0]["staleness"]["overall"] == "stale_critical"
    assert any(tc["staleness"]["status"] == "stale_critical" for tc in by_node[0]["test_cases"])

    # change summary endpoints
    ch = c.get(f"/nodes/{ids[0]}/changes").json()
    assert ch["status"] == "changed_in_latest" and ch["diff_to_latest"]["classification"] == "critical"
    d = c.get("/versions/1/diff/2").json()
    assert [m["section"] for m in d["modified"]] == [
        "2.1.1.1 Battery Life Under Typical Use", "3.2 Cuff Inflation Sequence", "4.2 Error Codes", "4.3 Alarm Thresholds"]
    assert [a["section"] for a in d["added"]] == ["5.3 Data Export"]

    # regenerate keeps history and supersedes the old one
    re_ = c.post(f"/selections/{sel['id']}/generate", json={"regenerate": True}).json()
    assert re_["id"] != gen["id"] and re_["reused"] is False
    assert c.get(f"/generations/{gen['id']}").json()["superseded_by"] == re_["id"]


def test_browse_search_and_node_detail(env):
    c, _ = env
    ingest(c, "ct200_manual_v1.pdf")
    secs = c.get("/sections").json()
    assert [s["number"] for s in secs["sections"]] == [str(i) for i in range(1, 9)]
    n = c.get(f"/nodes/{find(c, 1, '3')}").json()
    assert [k["number"] for k in n["children"]] == ["3.1", "3.2", "3.4", "3.3"]  # document order
    hits = c.get("/search", params={"q": "error codes", "scope": "headings"}).json()
    assert hits["count"] == 2
    assert c.get("/search", params={"q": "100%", "scope": "text"}).json()["count"] == 0  # % is escaped, not a wildcard
    assert c.get("/nodes/99999").status_code == 404


def test_selection_validation(env):
    c, _ = env
    ingest(c, "ct200_manual_v1.pdf")
    assert c.post("/selections", json={"name": "x", "node_ids": [999999]}).status_code == 422
    assert c.post("/selections", json={"name": "x", "node_ids": []}).status_code == 422


def test_ingest_rejects_path_traversal_and_non_pdf(env):
    c, _ = env
    assert c.post("/ingest", json={"file_path": "../app/main.py"}).status_code == 400
    assert c.post("/ingest", json={"file_path": "nope.pdf"}).status_code == 404
