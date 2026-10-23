"""Document store for LLM generations.

Justification for a JSON-file store instead of MongoDB: generations are append-mostly,
self-contained documents (snapshot + test cases + raw attempts) queried only by
selection id / logical node id / id. At this scale a directory of JSON files gives the
same document model with zero infrastructure and atomic writes (tmp + rename). The
interface below is small so a MongoDB implementation is a drop-in replacement.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path


class JsonFileStore:
    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def new_id(self) -> str:
        return uuid.uuid4().hex

    def _path(self, gen_id: str) -> Path:
        if not gen_id.isalnum():  # ids are hex; refuse path tricks
            raise KeyError(gen_id)
        return self.dir / f"{gen_id}.json"

    def save(self, doc: dict) -> None:
        with self._lock:
            tmp = self._path(doc["id"]).with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path(doc["id"]))

    def get(self, gen_id: str) -> dict | None:
        try:
            p = self._path(gen_id)
        except KeyError:
            return None
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def _all(self) -> list[dict]:
        out = []
        for p in self.dir.glob("*.json"):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                continue  # a corrupt file must not break listing
        return sorted(out, key=lambda d: d["created_at"])

    def by_selection(self, selection_id: int) -> list[dict]:
        return [d for d in self._all() if d["selection_id"] == selection_id]

    def by_logical_node(self, logical_node_id: int) -> list[dict]:
        return [d for d in self._all()
                if any(s["logical_node_id"] == logical_node_id for s in d["snapshot"])]

    def find_ok_by_fingerprint(self, fingerprint: str) -> dict | None:
        hits = [d for d in self._all() if d["fingerprint"] == fingerprint
                and d["status"] == "ok" and not d.get("superseded_by")]
        return hits[-1] if hits else None
