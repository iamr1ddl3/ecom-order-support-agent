"""
Rate-limit retry check (no LLM, no network).

_with_retry decides whether the regression gate reports a quota pause as a
regression, so it needs its own check: a 429 must be retried, a real error must
surface immediately, and the server's own "try again in Ns" hint must be
honoured rather than ignored in favour of a guess.

Run: python -m agent.test_retry
"""

import agent.providers as providers


class RateLimitError(Exception):
    """Mimics the SDK exception, which is matched by class name."""


def demo() -> None:
    sleeps: list[float] = []
    providers.time = type("t", (), {"sleep": staticmethod(lambda s: sleeps.append(s))})()

    # 1. succeeds after two rate limits
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("Error code: 429 - rate_limit_exceeded. Please try again in 4.5s")
        return "ok"

    assert providers._with_retry(flaky) == "ok", "should succeed after retries"
    assert calls["n"] == 3, f"expected 3 attempts, got {calls['n']}"
    print(f"[ok] retried a 429 twice, then succeeded (waits: {sleeps})")

    # 2. honours the server's stated wait rather than guessing
    assert sleeps and abs(sleeps[0] - 5.0) < 0.01, f"expected ~5.0s (4.5 + 0.5), got {sleeps[0]}"
    print(f"[ok] honoured the server's 'try again in 4.5s' hint -> slept {sleeps[0]}s")

    # 3. a non-rate-limit error is raised immediately, NOT retried
    attempts = {"n": 0}

    def broken():
        attempts["n"] += 1
        raise ValueError("malformed request")

    try:
        providers._with_retry(broken)
        raise AssertionError("should have raised ValueError")
    except ValueError:
        pass
    assert attempts["n"] == 1, f"a real error must not be retried, got {attempts['n']} attempts"
    print("[ok] a non-rate-limit error raises immediately, no retry")

    # 4. gives up rather than looping forever
    forever = {"n": 0}

    def always_limited():
        forever["n"] += 1
        raise RateLimitError("429 rate_limit_exceeded")

    try:
        providers._with_retry(always_limited, attempts=3)
        raise AssertionError("should have raised after exhausting attempts")
    except RateLimitError:
        pass
    assert forever["n"] == 3, f"expected exactly 3 attempts, got {forever['n']}"
    print("[ok] gives up after the attempt limit instead of hanging")

    print("\nAll retry checks passed.")


if __name__ == "__main__":
    demo()
