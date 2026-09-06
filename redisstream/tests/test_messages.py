"""
message 검증 테스트.

여기서 걸러지지 않는 형식 오류는 실행 중에 예외가 되어
PEL을 점유한 채 회수만 반복하게 된다.
"""

import json

import pytest

from app.common.messages import InvalidMessage, JobMessage


def _payload(**overrides) -> dict[str, str]:
    payload = {"name": "t", "steps": 3, "step_delay_sec": 1.5, "data": {}}
    payload.update(overrides)
    return {"job_id": "j1", "payload": json.dumps(payload)}


def test_parses_valid_message():
    msg = JobMessage.parse(_payload())
    assert msg == JobMessage(job_id="j1", steps=3, step_delay_sec=1.5)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"payload": "{}"}, "job_id"),
        ({"job_id": "", "payload": "{}"}, "job_id"),
        ({"job_id": "j1"}, "payload 필드 없음"),
        ({"job_id": "j1", "payload": "NOT-JSON"}, "JSON 파싱 실패"),
        ({"job_id": "j1", "payload": "[1,2]"}, "object가 아님"),
        ({"job_id": "j1", "payload": '{"steps": 1}'}, "필수 필드 누락"),
        ({"job_id": "j1", "payload": '{"step_delay_sec": 1}'}, "필수 필드 누락"),
    ],
)
def test_rejects_malformed(data, match):
    with pytest.raises(InvalidMessage, match=match):
        JobMessage.parse(data)


def test_rejects_bad_types():
    with pytest.raises(InvalidMessage, match="타입 오류"):
        JobMessage.parse(_payload(steps="셋"))


@pytest.mark.parametrize("steps", [0, -1])
def test_rejects_nonpositive_steps(steps):
    with pytest.raises(InvalidMessage, match="steps는 1 이상"):
        JobMessage.parse(_payload(steps=steps))


def test_rejects_negative_delay():
    with pytest.raises(InvalidMessage, match="step_delay_sec"):
        JobMessage.parse(_payload(step_delay_sec=-1))


def test_coerces_numeric_strings():
    # stream 필드는 전부 문자열로 오므로 숫자 문자열은 받아들인다.
    msg = JobMessage.parse(_payload(steps="4", step_delay_sec="0.5"))
    assert msg.steps == 4
    assert msg.step_delay_sec == 0.5
