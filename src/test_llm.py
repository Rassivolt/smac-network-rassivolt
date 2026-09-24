"""
test_llm.py - the retry policy, the one part of llm.py with a decision in it.

    python test_llm.py          # no key, no network, no server, no waiting

A 429 means two different things and they need opposite responses: a rate limit
clears in seconds and is worth waiting out, while the month's credit running out
does not clear until the month does, and retrying it spends the tool's time
budget to be told the same thing again. Every test here is about telling those
apart, so none of them is allowed to touch the network or to actually sleep -
the sleeps are recorded and asserted on instead.

Written as plain asserts in test_* functions, so it runs under pytest if you
have it and under `python test_llm.py` if you do not.
"""

from __future__ import annotations

import os

import llm


class Reply:
    """The parts of an httpx response that llm.ask reads."""

    def __init__(self, status_code: int, text: str = "", headers: dict | None = None,
                 payload: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


OK = Reply(200, payload={"content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 10, "output_tokens": 5}})
RATE_LIMITED = Reply(429, text='{"error": {"message": "rate_limit_error"}}')
OUT_OF_CREDIT = Reply(429, text='{"error": {"message": "Your credit balance is too low"}}')
REJECTED_KEY = Reply(401, text='{"error": {"message": "invalid x-api-key"}}')


def ask_against(replies: list[Reply], user: str = "user") -> tuple[object, list, list]:
    """Answer ask() from a scripted list. Returns (result or error, calls, sleeps).

    The last reply is repeated if ask() asks again, so a test can script "always
    rate limited" with a single entry.
    """
    calls: list[float] = []
    sleeps: list[float] = []
    saved = (llm.httpx.post, llm.time.sleep, llm._log, os.environ.get(llm.API_KEY))

    def fake_post(*_args, **kwargs):
        calls.append(kwargs.get("timeout"))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    llm.httpx.post = fake_post
    llm.time.sleep = sleeps.append              # recorded, never waited
    llm._log = lambda *a, **k: None             # no usage.jsonl line from a test
    os.environ[llm.API_KEY] = "test-key-not-a-real-one"
    try:
        return llm.ask("system", user), calls, sleeps
    except RuntimeError as exc:
        return exc, calls, sleeps
    finally:
        llm.httpx.post, llm.time.sleep, llm._log = saved[:3]
        if saved[3] is None:
            os.environ.pop(llm.API_KEY, None)
        else:
            os.environ[llm.API_KEY] = saved[3]


def test_no_key_is_a_valid_state_and_never_calls_out():
    saved = os.environ.pop(llm.API_KEY, None)
    try:
        assert llm.ask("system", "user") == llm.NO_KEY
    finally:
        if saved is not None:
            os.environ[llm.API_KEY] = saved


def test_a_rate_limit_is_waited_out_and_the_answer_still_arrives():
    result, calls, sleeps = ask_against([RATE_LIMITED, OK])
    assert result == "done"
    assert len(calls) == 2, "the second attempt is what makes this worth doing"
    assert sleeps == [1.0], "first backoff is a second"


def test_the_servers_own_retry_after_wins_over_the_backoff():
    result, _calls, sleeps = ask_against([Reply(429, text="slow down", headers={"retry-after": "3"}), OK])
    assert result == "done" and sleeps == [3.0]


def test_running_out_of_credit_is_not_retried():
    # The lecture's case: a 429 that will not clear until the month does.
    # Retrying it cannot succeed, and every attempt costs the caller time it
    # could have spent being told the truth.
    result, calls, sleeps = ask_against([OUT_OF_CREDIT])
    assert isinstance(result, RuntimeError) and "429" in str(result)
    assert len(calls) == 1 and sleeps == []


def test_a_rejected_key_is_not_retried():
    result, calls, sleeps = ask_against([REJECTED_KEY])
    assert isinstance(result, RuntimeError) and "401" in str(result)
    assert len(calls) == 1 and sleeps == []


def test_retrying_gives_up_inside_the_tool_budget():
    # A tool that answers after the caller has given up has failed anyway, so
    # the retries are bounded by both a count and a wall-clock window.
    result, calls, sleeps = ask_against([RATE_LIMITED])
    assert isinstance(result, RuntimeError)
    assert len(calls) <= llm.RETRY_ATTEMPTS
    assert sum(sleeps) < llm.RETRY_WINDOW_SECONDS
    # Every request is also given less time than the window that contains it.
    assert all(timeout <= llm.TIMEOUT_SECONDS for timeout in calls)


def test_an_empty_reply_is_a_fault_not_an_answer():
    empty = Reply(200, payload={"content": [{"type": "text", "text": "   "}], "usage": {}})
    result, calls, _sleeps = ask_against([empty])
    assert isinstance(result, RuntimeError) and "no text" in str(result)
    assert len(calls) == 1, "a complete reply that says nothing is not worth asking twice"


def test_a_prompt_the_size_of_the_console_router_is_sent():
    # The console's router prompt carries every agent's tool catalogue and is
    # about 19,400 characters. A ceiling of 8,000 refused it, and every
    # model-routed console query silently fell back to the scripted plan.
    result, calls, _ = ask_against([OK], user="x" * 30_000)
    assert result == "done" and len(calls) == 1


def test_a_runaway_prompt_is_refused_before_anything_is_spent():
    result, calls, _ = ask_against([OK], user="x" * (llm.MAX_PROMPT_CHARS + 1))
    assert isinstance(result, RuntimeError) and "limit" in str(result)
    assert calls == [], "the ceiling must refuse before the request is made"


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as exc:
            failures.append(name)
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
