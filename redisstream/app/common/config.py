"""
환경변수 기반 설정.

운영 상황(job 길이, 배포 주기, 백로그 규모)에 따라 바뀌어야 하는 값은
전부 여기 모아 env로 노출한다. 코드 어디에서도 os.environ을 직접 읽지 않는다.
"""

import os
from dataclasses import dataclass


class ConfigError(Exception):
    """환경변수 값이 잘못되어 기동할 수 없는 상태."""


def _raw(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _int(name: str, default: int) -> int:
    raw = _raw(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: 정수가 필요하다 (받은 값: {raw!r})") from exc


def _float(name: str, default: float) -> float:
    raw = _raw(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: 실수가 필요하다 (받은 값: {raw!r})") from exc


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name, "true" if default else "false").lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name}: bool이 필요하다 (받은 값: {raw!r})")


@dataclass(frozen=True)
class Settings:
    # --- 연결 ---
    valkey_url: str = "redis://localhost:6379/0"

    # --- 키 이름 (환경/테넌트 분리용) ---
    stream: str = "jobs:stream"
    dead_stream: str = "jobs:dead"
    group: str = "jobs:workers"

    # --- 소비/회수 타이밍 ---
    # claim_idle_ms: 이만큼 idle인 PEL 항목을 다른 worker가 회수한다.
    #                job의 정상 실행 시간이 아니라 "worker 사망 감지 시간"으로 잡는다.
    claim_idle_ms: int = 30_000
    heartbeat_interval_sec: float = 10.0
    reclaim_interval_sec: float = 3.0
    read_block_ms: int = 3_000

    # --- 재시도 ---
    # 회수 허용 횟수. 초과하면 DLQ로 보낸다.
    max_retries: int = 5

    # --- 종료 ---
    # SIGTERM 이후 실행 중 job에 허용할 완주 시간.
    # 정상 job의 최대 실행 시간에 맞춘다. compose의 stop_grace_period는 이보다 커야 한다.
    shutdown_grace_sec: float = 60.0

    # --- janitor ---
    # 이만큼 idle이고 pending이 0인 consumer 등록을 제거 대상으로 본다.
    consumer_idle_ms: int = 300_000
    janitor_interval_sec: float = 60.0
    janitor_lock_ttl_sec: int = 55
    trim_enabled: bool = True

    # --- 보관 ---
    dead_maxlen: int = 10_000
    job_ttl_sec: int = 604_800  # 7일

    # --- API 입력 상한 ---
    max_steps: int = 100
    max_step_delay_sec: float = 3_600.0

    # --- 로깅 ---
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        self._validate()

    @property
    def janitor_lock_key(self) -> str:
        return f"{self.group}:janitor:lock"

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()  # 기본값을 한 곳에서만 선언하기 위해 기본 인스턴스를 참조한다.
        return cls(
            valkey_url=_raw("VALKEY_URL", d.valkey_url),
            stream=_raw("STREAM_KEY", d.stream),
            dead_stream=_raw("DEAD_STREAM_KEY", d.dead_stream),
            group=_raw("GROUP_NAME", d.group),
            claim_idle_ms=_int("CLAIM_IDLE_MS", d.claim_idle_ms),
            heartbeat_interval_sec=_float(
                "HEARTBEAT_INTERVAL_SEC", d.heartbeat_interval_sec
            ),
            reclaim_interval_sec=_float(
                "RECLAIM_INTERVAL_SEC", d.reclaim_interval_sec
            ),
            read_block_ms=_int("READ_BLOCK_MS", d.read_block_ms),
            max_retries=_int("MAX_RETRIES", d.max_retries),
            shutdown_grace_sec=_float("SHUTDOWN_GRACE_SEC", d.shutdown_grace_sec),
            consumer_idle_ms=_int("CONSUMER_IDLE_MS", d.consumer_idle_ms),
            janitor_interval_sec=_float(
                "JANITOR_INTERVAL_SEC", d.janitor_interval_sec
            ),
            janitor_lock_ttl_sec=_int(
                "JANITOR_LOCK_TTL_SEC", d.janitor_lock_ttl_sec
            ),
            trim_enabled=_bool("TRIM_ENABLED", d.trim_enabled),
            dead_maxlen=_int("DEAD_MAXLEN", d.dead_maxlen),
            job_ttl_sec=_int("JOB_TTL_SEC", d.job_ttl_sec),
            max_steps=_int("MAX_STEPS", d.max_steps),
            max_step_delay_sec=_float("MAX_STEP_DELAY_SEC", d.max_step_delay_sec),
            log_level=_raw("LOG_LEVEL", d.log_level).upper(),
        )

    def _validate(self) -> None:
        if not self.stream or not self.group or not self.dead_stream:
            raise ConfigError("STREAM_KEY / GROUP_NAME / DEAD_STREAM_KEY는 비울 수 없다")

        if self.stream == self.dead_stream:
            raise ConfigError("STREAM_KEY와 DEAD_STREAM_KEY는 달라야 한다")

        for name, value in (
            ("CLAIM_IDLE_MS", self.claim_idle_ms),
            ("READ_BLOCK_MS", self.read_block_ms),
            ("DEAD_MAXLEN", self.dead_maxlen),
            ("JOB_TTL_SEC", self.job_ttl_sec),
            ("JANITOR_LOCK_TTL_SEC", self.janitor_lock_ttl_sec),
            ("MAX_STEPS", self.max_steps),
        ):
            if value <= 0:
                raise ConfigError(f"{name}는 0보다 커야 한다 (받은 값: {value})")

        for name, value in (
            ("HEARTBEAT_INTERVAL_SEC", self.heartbeat_interval_sec),
            ("RECLAIM_INTERVAL_SEC", self.reclaim_interval_sec),
            ("JANITOR_INTERVAL_SEC", self.janitor_interval_sec),
            ("MAX_STEP_DELAY_SEC", self.max_step_delay_sec),
        ):
            if value <= 0:
                raise ConfigError(f"{name}는 0보다 커야 한다 (받은 값: {value})")

        if self.max_retries < 0:
            raise ConfigError(f"MAX_RETRIES는 0 이상이어야 한다 (받은 값: {self.max_retries})")

        if self.shutdown_grace_sec < 0:
            raise ConfigError("SHUTDOWN_GRACE_SEC는 0 이상이어야 한다")

        # heartbeat가 claim_idle 안에 최소 2회 실행되지 않으면
        # 정상 실행 중인 job이 다른 worker에게 회수될 수 있다.
        if self.heartbeat_interval_sec * 2_000 > self.claim_idle_ms:
            raise ConfigError(
                "HEARTBEAT_INTERVAL_SEC * 2000 <= CLAIM_IDLE_MS 여야 한다 "
                f"(heartbeat={self.heartbeat_interval_sec}s, "
                f"claim_idle={self.claim_idle_ms}ms). "
                "그렇지 않으면 정상 job이 회수될 수 있다."
            )

        # 살아 있는 worker의 consumer 등록이 스윕에 지워지지 않도록 여유를 둔다.
        if self.consumer_idle_ms < self.claim_idle_ms * 2:
            raise ConfigError(
                "CONSUMER_IDLE_MS >= CLAIM_IDLE_MS * 2 여야 한다 "
                f"(consumer_idle={self.consumer_idle_ms}ms, "
                f"claim_idle={self.claim_idle_ms}ms)"
            )

        # 락이 주기보다 오래 살아 있으면 janitor가 영영 돌지 못한다.
        if self.janitor_lock_ttl_sec >= self.janitor_interval_sec:
            raise ConfigError(
                "JANITOR_LOCK_TTL_SEC < JANITOR_INTERVAL_SEC 여야 한다 "
                f"(lock_ttl={self.janitor_lock_ttl_sec}s, "
                f"interval={self.janitor_interval_sec}s)"
            )
