"""
Settings 검증 테스트.

잘못된 조합은 기동 시점에 막는다.
운영 중에 조용히 오동작하는 것보다 뜨지 않는 편이 낫다.
"""

import pytest

from app.common.config import ConfigError, Settings


def test_defaults_are_valid():
    s = Settings()
    assert s.stream != s.dead_stream
    assert s.janitor_lock_key.startswith(s.group)


def test_heartbeat_must_fit_inside_claim_idle():
    # heartbeat가 claim_idle 안에 2회 이상 돌지 못하면
    # 정상 실행 중인 job이 회수될 수 있다.
    with pytest.raises(ConfigError, match="HEARTBEAT_INTERVAL_SEC"):
        Settings(claim_idle_ms=10_000, heartbeat_interval_sec=6.0)

    Settings(claim_idle_ms=10_000, heartbeat_interval_sec=5.0)


def test_consumer_idle_must_exceed_claim_idle():
    # 살아 있는 worker의 등록이 스윕에 지워지면 안 된다.
    with pytest.raises(ConfigError, match="CONSUMER_IDLE_MS"):
        Settings(claim_idle_ms=30_000, consumer_idle_ms=30_000)

    Settings(claim_idle_ms=30_000, consumer_idle_ms=60_000)


def test_janitor_lock_must_expire_before_next_run():
    with pytest.raises(ConfigError, match="JANITOR_LOCK_TTL_SEC"):
        Settings(janitor_interval_sec=10.0, janitor_lock_ttl_sec=10)

    Settings(janitor_interval_sec=10.0, janitor_lock_ttl_sec=9)


def test_stream_and_dead_stream_must_differ():
    with pytest.raises(ConfigError, match="달라야"):
        Settings(stream="same", dead_stream="same")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"claim_idle_ms": 0},
        {"max_steps": 0},
        {"job_ttl_sec": -1},
        {"max_retries": -1},
        {"reclaim_interval_sec": 0},
    ],
)
def test_rejects_nonpositive_values(kwargs):
    with pytest.raises(ConfigError):
        Settings(**kwargs)


def test_from_env_reads_and_coerces(monkeypatch):
    monkeypatch.setenv("CLAIM_IDLE_MS", "45000")
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SEC", "5")
    monkeypatch.setenv("CONSUMER_IDLE_MS", "600000")
    monkeypatch.setenv("TRIM_ENABLED", "no")
    monkeypatch.setenv("STREAM_KEY", "custom:stream")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    s = Settings.from_env()

    assert s.claim_idle_ms == 45_000
    assert s.heartbeat_interval_sec == 5.0
    assert s.trim_enabled is False
    assert s.stream == "custom:stream"
    assert s.log_level == "DEBUG"


def test_from_env_rejects_garbage(monkeypatch):
    monkeypatch.setenv("MAX_RETRIES", "다섯")
    with pytest.raises(ConfigError, match="MAX_RETRIES"):
        Settings.from_env()


def test_from_env_treats_blank_as_unset(monkeypatch):
    monkeypatch.setenv("CLAIM_IDLE_MS", "   ")
    assert Settings.from_env().claim_idle_ms == Settings().claim_idle_ms
