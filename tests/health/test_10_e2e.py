"""Сквозные сценарии: запрос проходит всю цепочку и возвращает осмысленный ответ.

  core-api /search_pipeline:  core-api → LLM (классификация) → backend → Postgres/FAISS → ответ
  admin /chat/search:         admin → core-api → … (то же, что видит пользователь в чате админки)
"""
import pytest

from conftest import SLOW_TIMEOUT

pytestmark = [pytest.mark.slow, pytest.mark.llm]

QUERY = "Расскажи о байкальской нерпе"


def _answer(data):
    result = data.get("result") or {}
    return result.get("answer") if isinstance(result, dict) else None


def _root_cause(data):
    """core-api отвечает 200 даже при ошибке LLM — причина лежит в slots.error."""
    slots = data.get("slots") or {}
    return slots.get("error") if isinstance(slots, dict) else None


def test_core_api_search_pipeline(http, require):
    require("core-api")
    resp = http("core-api").post("/search_pipeline", json={"query": QUERY, "user_id": "healthcheck"},
                                 timeout=SLOW_TIMEOUT)
    assert resp.status_code == 200, "HTTP %s: %s" % (resp.status_code, resp.text[:300])
    data = resp.json()
    assert "error" not in data, data["error"]
    assert not _root_cause(data), "Классификация упала (LLM): %s" % _root_cause(data)
    answer = _answer(data)
    assert answer, "Пустой ответ пайплайна: %s" % str(data)[:400]
    assert "устарел" not in answer and "не готов" not in answer, answer[:200]


def test_admin_chat_search(http, require, admin_session):
    require("admin")
    resp = http("admin").post("/chat/search", json={"text": QUERY}, headers=admin_session, timeout=SLOW_TIMEOUT)
    assert resp.status_code == 200, resp.text[:300]
    data = resp.json()
    assert "error" not in data, data["error"]
    assert not _root_cause(data), "Классификация упала (LLM): %s" % _root_cause(data)
    assert _answer(data), "Пустой ответ чата админки: %s" % str(data)[:400]
