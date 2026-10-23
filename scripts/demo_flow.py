"""End-to-end demo against a RUNNING server: v1 -> selection -> generation -> v2 -> staleness.

    uvicorn app.main:create_app --factory --port 8000      # in another terminal
    python scripts/demo_flow.py [http://127.0.0.1:8000]

Run against a fresh DATABASE_URL / GENERATION_STORE_DIR for a clean demo.
"""
import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
c = httpx.Client(base_url=BASE, timeout=120)


def show(title, obj):
    print(f"\n=== {title}")
    print(json.dumps(obj, indent=2, ensure_ascii=False)[:1800])


def find(version, number):
    stack = [s["id"] for s in c.get("/sections", params={"version": version}).json()["sections"]]
    while stack:
        n = c.get(f"/nodes/{stack.pop()}").json()
        if n["number"] == number:
            return n["id"]
        stack += [k["id"] for k in n["children"]]


r = c.post("/ingest", json={"file_path": "ct200_manual_v1.pdf"}).json()
show("1. ingest v1 (note parser warnings: level skip, out-of-order, style deviation)",
     {k: r[k] for k in ("version", "node_count", "change_counts")} | {"warnings": [w["code"] + " " + str(w.get("number")) for w in r["warnings"]]})

ids = [find(1, n) for n in ("2.1.1.1", "3.2", "4.2", "5.1")]
sel = c.post("/selections", json={"name": "safety + battery sections", "node_ids": ids}).json()
show("2. version-pinned selection", sel)

gen = c.post(f"/selections/{sel['id']}/generate").json()
show("3. generated test cases (staleness right after generation)",
     {"id": gen["id"], "status": gen["status"], "overall": gen["staleness"]["overall"],
      "first_test_case": gen["test_cases"][0]})

again = c.post(f"/selections/{sel['id']}/generate").json()
show("4. duplicate submit -> reused, no new LLM call", {"id": again["id"], "reused": again["reused"]})

r = c.post("/ingest", json={"file_path": "ct200_manual_v2.pdf"}).json()
show("5. ingest v2 as a NEW version of the same document", {k: r[k] for k in ("version", "node_count", "change_counts")})
show("6. what changed v1 -> v2", {k: [x["section"] for x in v] if isinstance(v, list) else v
                                   for k, v in c.get("/versions/1/diff/2").json().items()})
show("7. node-level change + diff for 4.2 (v1 node)", c.get(f"/nodes/{ids[2]}/changes").json()["diff_to_latest"])
pinned = c.get(f"/selections/{sel['id']}").json()
show("8. old selection still resolves to v1 text", {i["number"]: (i["version"], i["text"][:70]) for i in pinned["items"]})

g = c.get(f"/selections/{sel['id']}/generations").json()[0]
show("9. SAME generation retrieved after v2: staleness per node", [
    {"section": n["section"], "status": n["status"], "critical_removed": (n["diff"] or {}).get("critical_removed"),
     "critical_added": (n["diff"] or {}).get("critical_added"), "upstream": n["upstream_changes"]} for n in g["staleness"]["per_node"]])
show("10. per test case verdicts (retrieval by node id works too)", [
    {"title": t["title"][:50], "status": t["staleness"]["status"]} for t in c.get(f"/nodes/{ids[0]}/generations").json()[0]["test_cases"]])
print("\nlimits:", g["staleness"]["limits"])
