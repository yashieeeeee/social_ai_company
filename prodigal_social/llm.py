"""Thin Ollama wrapper: localhost only, JSON enforcement, retry, fallback, accounting.

Design: single model (qwen2.5:3b) for all agents. No hosted APIs anywhere --
only http://localhost:11434. Unit-testable via injected request_fn.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

LOG = logging.getLogger("prodigal.llm")

OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:3b"

JSON_ONLY_INSTR = (
    "\n\nRespond with VALID JSON only. No markdown fences, no commentary. "
    "If a schema is implied, match its keys exactly."
)

# Matches the largest {...} block for salvage repair.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class LLMResult:
    text: str
    data: Optional[Dict[str, Any]] = None
    tokens_in: int = 0
    tokens_out: int = 0
    latency_s: float = 0.0
    attempts: int = 1
    fallback_used: bool = False
    error: str = ""
    model: str = DEFAULT_MODEL


@dataclass
class UsageLedger:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    fallbacks: int = 0
    entries: List[Dict[str, Any]] = field(default_factory=list)

    def record(self, r: LLMResult, agent: str = "") -> None:
        self.calls += 1
        self.tokens_in += r.tokens_in
        self.tokens_out += r.tokens_out
        if r.fallback_used:
            self.fallbacks += 1
        self.entries.append(
            {
                "agent": agent,
                "model": r.model,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "latency_s": round(r.latency_s, 2),
                "attempts": r.attempts,
                "fallback": r.fallback_used,
                "error": r.error[:200],
            }
        )

    def summary(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "fallbacks": self.fallbacks,
        }


def _extract_json_block(text: str) -> Optional[str]:
    """Salvage: strip fences, find largest {...}."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    m = _JSON_BLOCK.search(t)
    return m.group(0) if m else None


def validate_required(data: Dict[str, Any], required: List[str]) -> List[str]:
    """Minimal schema check without extra deps. Returns missing-key list."""
    if not isinstance(data, dict):
        return ["<not-a-dict>"]
    return [k for k in required if k not in data]


class OllamaClient:
    """Callable wrapper over Ollama /api/generate.

    request_fn signature: (url, payload, timeout) -> dict (parsed JSON body).
    Inject a fake in tests so no running model is needed.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = OLLAMA_HOST,
        timeout_s: int = 120,
        num_ctx: int = 2048,
        max_retries: int = 3,
        log_path: Optional[Path] = None,
        request_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> None:
        if host != OLLAMA_HOST and "localhost" not in host and "127.0.0.1" not in host:
            raise ValueError(f"Refusing non-local Ollama host: {host} (local-only policy)")
        self.model = model
        self.host = host
        self.timeout_s = timeout_s
        self.num_ctx = num_ctx
        self.max_retries = max_retries
        self.usage = UsageLedger()
        self.log_path = log_path
        self._request_fn = request_fn or self._http_request

    # -- low-level HTTP (only place that touches the network) --
    def _http_request(self, url: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        import requests  # local import so tests don't need it when mocked

        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        # Non-streaming generate returns one JSON object.
        return resp.json()

    def _raw_complete(
        self, prompt: str, system: str, temperature: float, stream: bool = False
    ) -> tuple[str, int, int]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system or "",
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        body = self._request_fn(f"{self.host}/api/generate", payload, self.timeout_s)
        text = body.get("response", "") or ""
        # Ollama reports real counts; fakes may omit -> approximate.
        tok_in = int(body.get("prompt_eval_count") or max(1, len(prompt.split()) * 3 // 4))
        tok_out = int(body.get("eval_count") or max(1, len(text.split()) * 3 // 4))
        return text, tok_in, tok_out

    def stream(
        self, prompt: str, system: str = "", temperature: float = 0.2
    ) -> Iterator[str]:
        """Streaming support: yields text chunks, still localhost-only."""
        try:
            text, _, _ = self._raw_complete(prompt, system, temperature)
        except Exception as e:  # never let the viewer crash the run
            LOG.warning("LLM stream failed: %s", e)
            return
        # Chunked yield emulates token streaming for the CLI viewer.
        # (True SSE streaming is avoided: harder to unit-test, no benefit on CPU.)
        for i in range(0, max(1, len(text)), 120):
            yield text[i : i + 120]

    # -- high-level: text + JSON with repair loop --
    def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        agent: str = "",
    ) -> LLMResult:
        t0 = time.time()
        try:
            text, tin, tout = self._raw_complete(prompt, system, temperature)
            r = LLMResult(text=text, tokens_in=tin, tokens_out=tout,
                          latency_s=time.time() - t0, model=self.model)
        except Exception as e:  # connection refused, timeout, etc.
            r = LLMResult(text="", error=f"ollama-call-failed: {e}",
                          latency_s=time.time() - t0, fallback_used=True, model=self.model)
            LOG.warning("LLM call failed (%s): %s", agent, e)
            self._log(r, agent)
            self.usage.record(r, agent)
            return r
        self._log(r, agent)
        self.usage.record(r, agent)
        return r

    def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.1,
        required: Optional[List[str]] = None,
        fallback: Optional[Dict[str, Any]] = None,
        agent: str = "",
    ) -> LLMResult:
        """Enforce structured output.

        Strategy: ask-for-JSON -> parse -> salvage {..} block -> repair-retry
        (re-prompt with the broken output + 'fix this JSON') up to max_retries.
        Then degrade to caller-supplied fallback (never crash).
        """
        t0 = time.time()
        last_err = ""
        text = ""
        tin = tout = 0
        attempts = 0
        current = prompt + JSON_ONLY_INSTR

        for i in range(1, self.max_retries + 1):
            attempts = i
            try:
                text, tin, tout = self._raw_complete(current, system, temperature)
            except Exception as e:
                last_err = f"ollama-call-failed: {e}"
                LOG.warning("LLM JSON call failed attempt %d (%s): %s", i, agent, e)
                break  # transport failure: no point retrying instantly -> fallback
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                block = _extract_json_block(text)
                if block is not None:
                    try:
                        data = json.loads(block)
                    except json.JSONDecodeError as e:
                        last_err = f"malformed-json: {e} | raw={text[:160]!r}"
                        current = (
                            f"Your last output was NOT valid JSON:\n{text[:1500]}\n"
                            f"Error: {e}\nFix it. Return valid JSON only."
                        )
                        continue
                else:
                    last_err = f"malformed-json: no object found | raw={text[:160]!r}"
                    current = (
                        f"Your last output contained no JSON object:\n{text[:1500]}\n"
                        "Return valid JSON only."
                    )
                    continue
            missing = validate_required(data, required or [])
            if missing:
                last_err = f"schema-missing-keys={missing}"
                current = (
                    f"Your JSON is missing required keys {missing}. "
                    f"Last output:\n{json.dumps(data)[:1500]}\nReturn complete valid JSON only."
                )
                continue
            r = LLMResult(text=text, data=data, tokens_in=tin, tokens_out=tout,
                          latency_s=time.time() - t0, attempts=attempts, model=self.model)
            self._log(r, agent)
            self.usage.record(r, agent)
            return r

        # Exhausted retries (or transport failure) -> graceful fallback.
        fb = dict(fallback or {})
        fb.setdefault("_fallback", True)
        fb.setdefault("_why", last_err or "unknown")
        r = LLMResult(text=text, data=fb, tokens_in=tin, tokens_out=tout,
                      latency_s=time.time() - t0, attempts=attempts,
                      fallback_used=True, error=last_err, model=self.model)
        LOG.warning("LLM JSON fallback (%s): %s", agent, last_err)
        self._log(r, agent)
        self.usage.record(r, agent)
        return r

    def _log(self, r: LLMResult, agent: str) -> None:
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "agent": agent, "model": r.model,
                    "tin": r.tokens_in, "tout": r.tokens_out,
                    "attempts": r.attempts, "fallback": r.fallback_used,
                    "error": r.error[:200],
                    "text_hash": hashlib.sha256(r.text.encode()).hexdigest()[:12],
                }) + "\n")
        except OSError:
            pass
