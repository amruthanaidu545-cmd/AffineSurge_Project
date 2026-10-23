"""LLM clients. Any OpenAI-compatible endpoint (Groq, OpenRouter, Gemini's compat API) or a mock."""
from __future__ import annotations

import json
import os
import re
import time

import httpx


class LLMTransportError(Exception):
    """Network / HTTP / provider failure (not a content problem)."""


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, json_mode: bool = True,
                 timeout: float = 45.0):
        self.base_url, self.api_key, self.model = base_url.rstrip("/"), api_key, model
        self.json_mode, self.timeout, self.name = json_mode, timeout, "openai_compat"

    def complete(self, messages: list[dict]) -> str:
        payload = {"model": self.model, "messages": messages, "temperature": 0.2}
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        last = None
        for attempt in range(3):  # transport-level retry only: 429 / 5xx / timeouts
            try:
                r = httpx.post(f"{self.base_url}/chat/completions", json=payload, timeout=self.timeout,
                               headers={"Authorization": f"Bearer {self.api_key}"})
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}"
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"] or ""
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = repr(e)
                time.sleep(1.5 * (attempt + 1))
            except (httpx.HTTPStatusError, KeyError, IndexError, ValueError) as e:
                raise LLMTransportError(f"provider error: {e!r}") from e
        raise LLMTransportError(f"provider unavailable after retries: {last}")


class MockClient:
    """Deterministic offline LLM. `script` = list of behaviours consumed per call
    ('good' | 'fenced' | 'prose' | 'truncated' | 'bad_schema' | 'bad_citation' | 'too_many' | 'transport').
    When the script is exhausted the last entry repeats. Used by tests and the offline demo."""

    def __init__(self, script: list[str] | None = None):
        self.script, self.calls, self.name, self.model = script or ["good"], 0, "mock", "mock-qa-1"

    def complete(self, messages: list[dict]) -> str:
        mode = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user" and "<<<NODE" in m["content"]) \
            if any("<<<NODE" in m["content"] for m in messages) else messages[0]["content"]
        nodes = re.findall(r"<<<NODE id=(\d+) section=\"([^\"]*)\">>>\n(.*?)\n<<<END NODE>>>", user, re.S)
        if mode == "transport":
            raise LLMTransportError("mock transport failure")
        if mode == "prose":
            return "Sure! Here are some test ideas: check the device works."
        cases = []
        sentences = []
        for nid, sec, text in nodes:
            for s in re.split(r"(?<=[.;])\s+|\n", text):
                if s.strip() and len(s.strip()) > 15:
                    sentences.append((int(nid), sec, s.strip()))
        sentences.sort(key=lambda t: -len(re.findall(r"\d", t[2])))
        n = 6 if mode == "too_many" else 4
        for i in range(n):
            nid, sec, s = sentences[i % len(sentences)]
            cases.append({
                "title": f"Verify: {s[:70]}", "objective": f"Confirm behaviour stated in section {sec}",
                "preconditions": ["Device powered on with fresh batteries"],
                "steps": [f"Exercise the condition described in section {sec}", "Observe the device"],
                "expected_result": s, "priority": "high" if re.search(r"\d", s) else "medium",
                "source_node_ids": [999999 if mode == "bad_citation" else nid]})
        if mode == "bad_schema":
            for c in cases:
                c.pop("expected_result")
        out = json.dumps({"test_cases": cases}, indent=1)
        if mode == "fenced":
            return f"```json\n{out}\n```"
        if mode == "truncated":
            return out[: len(out) // 2]
        return out


def client_from_env():
    provider = os.getenv("LLM_PROVIDER", "mock")
    if provider == "mock":
        return MockClient()
    return OpenAICompatClient(os.environ["LLM_BASE_URL"], os.environ.get("LLM_API_KEY", ""),
                              os.environ["LLM_MODEL"], os.getenv("LLM_JSON_MODE", "1") == "1")
