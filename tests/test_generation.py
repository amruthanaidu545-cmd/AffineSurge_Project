"""LLM misbehaviour policy: retry with repair, then fail loudly without storing partial output."""
import shutil

import pytest
from fastapi.testclient import TestClient

from app.llm import MockClient
from app.main import create_app


def setup(tmp_path, script):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copy("data/ct200_manual_v1.pdf", data / "ct200_manual_v1.pdf")
    llm = MockClient(script)
    c = TestClient(create_app("sqlite:///:memory:", str(tmp_path / "g"), str(data), llm))
    assert c.post("/ingest", json={"file_path": "ct200_manual_v1.pdf"}).status_code == 201
    r = c.get("/search", params={"q": "Error Codes", "scope": "headings"}).json()["results"]
    sel = c.post("/selections", json={"name": "s", "node_ids": [r[0]["id"]]}).json()
    return c, llm, sel["id"]


@pytest.mark.parametrize("bad", ["prose", "truncated", "bad_schema", "bad_citation", "too_many"])
def test_malformed_output_is_repaired_by_retry(tmp_path, bad):
    c, llm, sid = setup(tmp_path, [bad, "good"])
    r = c.post(f"/selections/{sid}/generate")
    assert r.status_code == 200 and r.json()["status"] == "ok" and llm.calls == 2
    assert len(r.json()["attempts"]) == 2 and r.json()["attempts"][0]["error"]


def test_markdown_fenced_json_is_accepted(tmp_path):
    c, llm, sid = setup(tmp_path, ["fenced"])
    assert c.post(f"/selections/{sid}/generate").status_code == 200 and llm.calls == 1


def test_persistent_garbage_returns_502_and_stores_no_test_cases(tmp_path):
    c, llm, sid = setup(tmp_path, ["bad_schema"])
    r = c.post(f"/selections/{sid}/generate")
    assert r.status_code == 502 and llm.calls == 3 and r.json()["error"]["type"] == "invalid_output"
    assert c.get(f"/selections/{sid}/generations").json() == []  # hidden by default
    failed = c.get(f"/selections/{sid}/generations", params={"include_failed": True}).json()
    assert failed[0]["status"] == "failed" and failed[0]["test_cases"] == [] and len(failed[0]["attempts"]) == 3
    # a failed record does not block a later successful retry (not reused)
    llm.script, llm.calls = ["good"], 0
    assert c.post(f"/selections/{sid}/generate").json()["status"] == "ok"


def test_provider_outage_returns_503(tmp_path):
    c, llm, sid = setup(tmp_path, ["transport"])
    r = c.post(f"/selections/{sid}/generate")
    assert r.status_code == 503 and llm.calls == 1  # transport errors are not "repaired"
