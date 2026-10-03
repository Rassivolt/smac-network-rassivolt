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
is your cost dataset for the report; don't delete it. USAGE_LOG moves it: in
Docker it points at a mounted directory, because a file written inside the
container is lost the next time the container is replaced.

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
# USD per million tokens, input and output, at Anthropic's list prices as of
# Oct 2026. Per model, because model tier is an ablation variable and a cost
# column priced at Haiku's rate for every tier would understate the others by
# two to five times. A model missing here is logged with usd null rather than
# priced at a guess.
PRICES_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-5-5": (4.00, 20.00),
}

# Tiers asked to answer without extended thinking. Haiku 4.5 does not think
# unless asked, and the experiment's tier comparison is about the model, not
# about how long it deliberates - so the larger tiers, which think by default,
# are turned off to match. Sent only for these: Haiku's requests are unchanged,
# and the newest tiers (Sonnet 5.5, Opus 5.5) cannot turn thinking off at all,
# so they are not on the list and a result from them says so by its model name.
THINKING_OFF = frozenset({"claude-sonnet-5", "claude-opus-5"})

# Beside this file by default, which is right for a local run. Under Docker
# this points into a mounted directory instead: the container's own filesystem
# does not survive `up --force-recreate`, and a rebuild or an edited key is
# enough to trigger one. Losing a month of cost data to a routine restart is
# not a trade anyone would make knowingly.
USAGE_LOG = Path(os.environ.get("USAGE_LOG") or Path(__file__).with_name("usage.jsonl"))
TIMEOUT_SECONDS = 30.0

# A backstop on what any one call can cost - not the defence against hostile
# input, which is bounded where it enters (a requirement at 2,000 characters,
# at the tool boundary). Section 7 of the specification puts the budget on us
# and warns that nothing external stops a runaway loop, so a ceiling lives
# here, below every caller.
#
# It must sit above every LEGITIMATE prompt. At 8,000 it did not: the
# console's router prompt carries every agent's tool catalogue and runs to
# about 19,400 characters, so every model-routed query was refused and fell
# back to the scripted plan for two days before anyone noticed. 100,000
# characters is roughly 25,000 input tokens - about $0.03 on the default
# model - which still caps a single call, five times above the largest prompt
# anything here sends.
MAX_PROMPT_CHARS = 100_000

# A 429 means two different things and they need opposite responses. A rate
# limit clears in seconds; the month's credit running out does not clear until
# the month does, and retrying it spends the tool's time budget to arrive at
# the same answer. Two signals tell them apart, and both are used:
#
#   the `retry-after` header  the server's own statement that waiting will
#                             help. Present means transient, full stop.
#   the error type in the body  when no header is sent, Anthropic still names
#                             the error. `rate_limit_error` is worth retrying;
#                             anything about credit or billing is not.
#
# A 429 with neither signal is treated as not worth retrying. That is the
# conservative reading: a wasted wait at the Showcase is worse than a call that
# fails honestly, and the caller is told which it was.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 529})
_EXHAUSTED_MARKERS = ("credit balance", "billing", "spend limit", "quota")
_TRANSIENT_MARKERS = ("rate_limit", "overloaded", "too many requests")

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


def _worth_retrying(response) -> bool:
    """Whether waiting could plausibly change this answer.

    Anything other than a 429 that is in RETRYABLE_STATUS is a server-side
    fault and is retried. A 429 is decided on the two signals above: the
    header first, because it comes from the server, then the error type in the
    body, and a refusal that names credit or billing is never retried.
    """
    if response.status_code not in RETRYABLE_STATUS:
        return False
    text = response.text or ""
    if _out_of_credit(text):
        return False
    if response.status_code != 429:
        return True
    if _retry_after(response) is not None:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)

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
    request, the month's credit gone, or a 429 that carries neither a
    `retry-after` header nor a transient error type - raises on the first reply
    instead of spending the budget to be told the same thing three times.
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
    if model in THINKING_OFF:
        request["thinking"] = {"type": "disabled"}
    if len(system) + len(user) > MAX_PROMPT_CHARS:
        # Refuse before spending, and say by how much: a caller that can see
        # the limit can meet it, where a truncated answer would just be wrong
        # without saying so.
        raise RuntimeError(
            f"prompt is {len(system) + len(user)} characters against a limit of "
            f"{MAX_PROMPT_CHARS}; summarise the request before sending it."
        )

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
        if not _worth_retrying(response):
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
        price = PRICES_PER_MTOK.get(model)
        if price is not None:
            usd = round(tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1], 6)
    record = {
        "model": model,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "latency_ms": latency_ms,
        "usd": usd,
    }
    # The directory may not exist on the first call when USAGE_LOG points
    # somewhere new. Never let bookkeeping be the thing that fails a tool call.
    try:
        USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with USAGE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass
