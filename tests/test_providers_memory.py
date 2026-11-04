"""Error handling, rate limiting and the local database."""
import pytest

from tezau.memory import Store
from tezau.providers import AuthError, ProviderError, RateLimiter, classify_error


class HTTPError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize("error, kind", [
    (HTTPError("Unauthorized", 401), "auth"),
    (Exception("API key not valid. Please pass a valid API key."), "auth"),
    (HTTPError("model not found", 404), "fatal"),
    (Exception("Quota exceeded for metric GenerateRequestsPerDayPerProject"), "fatal"),
    (Exception("insufficient_quota: You exceeded your current quota"), "fatal"),
    (Exception("Your credit balance is too low"), "fatal"),
    (HTTPError("Too Many Requests", 429), "rate_limit"),
    (Exception("RESOURCE_EXHAUSTED: rate limit, retry in 30s"), "rate_limit"),
    (HTTPError("Service Unavailable", 503), "transient"),
    (TimeoutError("read timed out"), "transient"),
])
def test_classify_error(error, kind):
    assert classify_error(error) == kind


def test_rate_limiter_backs_off_and_recovers():
    limiter = RateLimiter(min_interval=0.0, max_interval=10.0)
    limiter.on_rate_limit()
    assert limiter.interval == 1.0
    limiter.on_rate_limit()
    assert limiter.interval == 2.0
    for _ in range(100):
        limiter.on_success()
    assert limiter.interval < 0.01


def test_transient_errors_are_retried(make_provider):
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) < 3:
            raise TimeoutError("read timed out")
        return f"[қаз] {text}"

    p = make_provider(translate=flaky)
    assert p.complete("system", "Сәлем", json_mode=False) == "[қаз] Сәлем"
    assert p.usage.requests == 1 and len(calls) == 3


def test_auth_and_fatal_errors_stop_at_once(make_provider):
    with pytest.raises(AuthError):
        make_provider(fail_after=0).complete("s", "Translate this page:\n{}")
    with pytest.raises(ProviderError):
        make_provider(fail_after=0, error=Exception("credit balance is too low")).complete(
            "s", "Translate this page:\n{}")


def test_missing_key_is_an_auth_error():
    from tezau.providers import get_provider
    with pytest.raises(AuthError):
        get_provider("gemini")


# ------------------------------------------------------------------ memory

def test_memory_roundtrip_and_normalization(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.tm_put_many([("Договор  поставки", "Жеткізу шарты", "gemini")])
    assert store.tm_get_many(["Договор поставки"]) == {"Договор поставки": "Жеткізу шарты"}
    assert store.tm_providers(["Договор поставки"])["Договор поставки"][1] == "gemini"


def test_machine_never_overwrites_human(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.tm_put_many([("заявки", "өтінімдер", None)], provider="human", human=True)
    store.tm_put_many([("заявки", "өтініштер", "openai")])
    assert store.tm_get_many(["заявки"])["заявки"] == "өтінімдер"


def test_human_term_is_never_revoted(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.term_put("заявки", "өтінімдер", status="human")
    store.term_put("заявки", "өтініштер", {"openai": "өтініштер"}, 1.0, "agreed")
    assert store.terms()["заявки"] == ("өтінімдер", "human")
