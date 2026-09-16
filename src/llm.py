"""
llm.py - one function, one model.

    from llm import ask
    text = ask(system="You are terse.", user="Explain this in one sentence.")

`ask()` is the only seam between your tools and a model. Set your key and it
works; there is nothing else to configure.

    ANTHROPIC_API_KEY=...

Choose a tier with LLM_MODEL, or per call:

    ask(system, user, model="claude-sonnet-5")

Model tier is one of the ablation options, so this is your independent variable.
Whichever you use is reported in health.llm_backend.

Every call appends a line to usage.jsonl - tokens, latency and cost. That file
is your cost dataset for the report; don't delete it.

A rate-limited call waits and tries again, inside a deadline that keeps the
whole thing within the tool budget. A call refused because the month's credit
is gone does not retry - see RETRYABLE_STATUS below.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_KEY = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-haiku-4-5"
PRICE_PER_MTOK = (1.00, 5.00)          # USD per million tokens: input, output

USAGE_LOG = Path(__file__).with_name("usage.jsonl")
TIMEOUT_SECONDS = 30.0

# A 429 means two different things and they need opposite responses. A rate
# limit clears in seconds, so the call waits and tries again - Anthropic sends
# `retry-after` when it knows how long to wait. The month's credit running out
# also arrives as a refusal, and that one does not clear until the month does:
# retrying it spends the tool's time budget to arrive at the same answer, so
# the response text is checked for the markers that tell them apart. A 529
# (overloaded) and the transient 5xx family are retried on the same schedule.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})
_EXHAUSTED_MARKERS = ("credit balance", "billing", "spend limit", "quota")

RETRY_ATTEMPTS = 3
# Retrying has a deadline rather than a fixed number of sleeps, because what
# matters is not how many times it tried but that the tool still answers: the
# specification allows a tool call 45s and smac.guard abandons it at 40s. A
# retry that would land outside this window is not attempted at all.
RETRY_WINDOW_SECONDS = 25.0


def _retry_after(response) -> float | None:
    """The server's own instruction on how long to wait, if it gave one."""
    try:
        return max(0.0, float(response.headers.get("retry-after")))
    except (TypeError, ValueError):
        return None


def _out_of_credit(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _EXHAUSTED_MARKERS)

NO_KEY = "(no API key configured - set ANTHROPIC_API_KEY to get written commentary)"


def configured() -> bool:
    """True if a key is present. Not configured is a valid state."""
    return bool(os.environ.get(API_KEY))


def model_name(model: str | None = None) -> str:
    """The model in use - reported in health.

    Never raises: health must answer even before a key is set, otherwise the
    Orchestrator's liveness probe fails for a reason unrelated to liveness.
    """
    if not configured():
        return "unconfigured"
    return model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL


def ask(system: str, user: str, max_tokens: int = 1024, model: str | None = None) -> str:
    """Send one prompt and return the text.

    With no key this returns NO_KEY rather than raising. The prose a tool adds is
    commentary on facts it has already computed, so losing the commentary must
    not take down the whole tool.

    A key that is present but rejected still raises: that is a real fault, and
    hiding it would waste your afternoon.

    A rate limit, an overloaded API or a transient 5xx is waited out and tried
    again, honouring `retry-after` when the server sends one, up to
    RETRY_ATTEMPTS and never past RETRY_WINDOW_SECONDS - a tool that answers
    late is worse than useless, because the caller has already given up. A
    refusal that will not clear on its own - a rejected key, a malformed
    request, the month's credit gone - raises on the first reply instead of
    spending the budget to be told the same thing three times.
    """
    if not configured():
        return NO_KEY

    model = model_name(model)
    request = {
        "model": model,
        "max_tokens": max_tokens,
        # cache_control marks the stable prefix so repeated calls are cheaper.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": os.environ[API_KEY], "anthropic-version": "2023-06-01"}

    deadline = time.monotonic() + RETRY_WINDOW_SECONDS
    failure = "anthropic: no attempt was made inside the retry window."

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        started = time.perf_counter()
        # Only the server's own "try again" is retried. A connection that died
        # mid-request might have been charged for and answered, and resending
        # it would pay twice for one answer, so that is left to raise.
        response = httpx.post(API_URL, headers=headers, json=request,
                              timeout=min(TIMEOUT_SECONDS, remaining))
        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code == 200:
            data = response.json()
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            if not text.strip():
                raise RuntimeError(f"{model} returned no text - try a larger max_tokens.")
            usage = data.get("usage", {})
            _log(model, usage.get("input_tokens"), usage.get("output_tokens"), latency_ms)
            return text

        failure = f"anthropic {response.status_code}: {response.text[:300]}"
        if response.status_code not in RETRYABLE_STATUS or _out_of_credit(response.text):
            break
        wait = _retry_after(response)
        if wait is None:
            wait = float(2 ** (attempt - 1))
        if attempt == RETRY_ATTEMPTS or time.monotonic() + wait >= deadline:
            break
        time.sleep(wait)

    raise RuntimeError(failure)


def _log(model: str, tokens_in: int | None, tokens_out: int | None, latency_ms: int) -> None:
    usd = None
    if tokens_in is not None and tokens_out is not None:
        usd = round(tokens_in / 1e6 * PRICE_PER_MTOK[0] + tokens_out / 1e6 * PRICE_PER_MTOK[1], 6)
    record = {
        "model": model,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "latency_ms": latency_ms,
        "usd": usd,
    }
    with USAGE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
