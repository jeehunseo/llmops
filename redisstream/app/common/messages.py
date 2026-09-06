"""
stream message의 형식 검증.

worker가 message를 실행하기 "전"에 형식을 확정한다.
실행 도중 KeyError/JSONDecodeError가 나면 그 message는 ACK되지도,
재시도 카운터에 반영되지도 못한 채 PEL을 영원히 점유할 수 있다.
"""

import json
from dataclasses import dataclass


class InvalidMessage(Exception):
    """재시도해도 결과가 동일한, 구조적으로 잘못된 message."""


@dataclass(frozen=True)
class JobMessage:
    job_id: str
    steps: int
    step_delay_sec: float

    @classmethod
    def parse(cls, data: dict[str, str]) -> "JobMessage":
        job_id = data.get("job_id")
        if not job_id:
            raise InvalidMessage("job_id 필드 없음")

        raw = data.get("payload")
        if raw is None:
            raise InvalidMessage("payload 필드 없음")

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidMessage(f"payload JSON 파싱 실패: {exc}") from exc

        if not isinstance(payload, dict):
            raise InvalidMessage(f"payload가 object가 아님: {type(payload).__name__}")

        try:
            steps = int(payload["steps"])
            step_delay_sec = float(payload["step_delay_sec"])
        except KeyError as exc:
            raise InvalidMessage(f"payload 필수 필드 누락: {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise InvalidMessage(f"payload 필드 타입 오류: {exc}") from exc

        if steps < 1:
            raise InvalidMessage(f"steps는 1 이상이어야 함: {steps}")

        if step_delay_sec < 0:
            raise InvalidMessage(f"step_delay_sec는 0 이상이어야 함: {step_delay_sec}")

        return cls(job_id=job_id, steps=steps, step_delay_sec=step_delay_sec)
