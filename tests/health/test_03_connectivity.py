"""Уровень 2: связи между сервисами — проверяются ИЗНУТРИ каждого сервиса, с его окружением.

Каждая строка матрицы = «сервис X умеет достучаться до Y так, как это делает его код»:
те же переменные (DB_HOST, ECOBOT_API_BASE_URL, CORE_API_BASE…), тот же драйвер, тот же DNS.
"""
import pytest

PG = {"type": "pg", "host": "${DB_HOST:-db}", "port": "${DB_PORT:-5432}", "db": "${DB_NAME:-eco}",
      "user": "${DB_USER:-postgres}", "password": "${DB_PASSWORD}"}
REDIS = {"type": "redis", "host": "${REDIS_HOST:-redis}", "port": "${REDIS_PORT:-6379}",
         "password": "${REDIS_PASSWORD}"}
# LLM через OpenAI-совместимый API (Ollama): список моделей должен содержать LLM_MODEL.
# Типичная ловушка: Ollama на хосте слушает 127.0.0.1, и из контейнера host.docker.internal:11434 закрыт.
LLM = {"type": "http", "url": "${LLM_BASE_URL}/models", "expect": [200], "body_contains": "${LLM_MODEL}",
       "skip_unless_env": "LLM_BASE_URL", "timeout": 15}

MATRIX = {
    "backend": [
        dict(PG, name="postgres"),
        dict(REDIS, name="redis"),
    ],
    "core-api": [
        dict(PG, name="postgres"),
        dict(REDIS, name="redis"),
        {"name": "backend", "type": "http", "url": "${ECOBOT_API_BASE_URL}/health", "expect": [200]},
        dict(LLM, name="llm"),
    ],
    "max": [
        dict(REDIS, name="redis"),
        {"name": "backend", "type": "http", "url": "${ECOBOT_API_BASE_URL}/health", "expect": [200]},
        dict(LLM, name="llm"),
    ],
    "admin": [
        dict(PG, name="postgres"),
        dict(REDIS, name="redis"),
        {"name": "core-api", "type": "http", "url": "${CORE_API_BASE:-http://core-api:5001}/openapi.json",
         "expect": [200]},
        {"name": "stt-service", "type": "http", "url": "${STT_URL:-http://stt-service:8081}/health",
         "expect": [200]},
        # без токена dsapi отвечает 403 — нам важно, что сервис по этому адресу есть
        {"name": "dsapi", "type": "http", "url": "${TEST_API_BASE}/status/healthcheck", "expect": [401, 403, 404]},
        # распознавание растений в чате админки (внешний сервис, адрес зашит по умолчанию в main.py)
        {"name": "plant-api", "type": "http", "url": "${PLANT_API_URL:-http://194.156.118.21:8020/predict}",
         "expect": "any", "timeout": 8},
    ],
    "dsapi": [
        # dsapi шлёт тестовые вопросы боту: BOT_DOMAIN + /test-api/test_query (bot_client.py)
        {"name": "bot (BOT_DOMAIN)", "type": "http", "url": "${BOT_DOMAIN}/test-api/test_query",
         "method": "POST", "headers": {"Content-Type": "application/json"}, "expect": [200, 400, 405, 422]},
        {"name": "ollama", "type": "http", "url": "${REMOTE_OLLAMA_URL}/api/tags", "expect": [200],
         "skip_unless_env": "REMOTE_OLLAMA_URL"},
    ],
    "annotator": [
        {"name": "nginx (IMAGE_BASE_URL)", "type": "http", "url": "${IMAGE_BASE_URL:-http://nginx}/health",
         "expect": "any"},
    ],
}

CASES = [(svc, check["name"]) for svc, checks in MATRIX.items() for check in checks]


@pytest.mark.parametrize("service,target", CASES, ids=["%s→%s" % c for c in CASES])
def test_service_reaches_dependency(stack, require, service, target):
    require(service)
    results = stack.probe(service, MATRIX[service])
    r = results[target]
    if r["skipped"]:
        pytest.skip(r["detail"])
    assert r["ok"], "%s не достучался до %s: %s" % (service, target, r["detail"])


def test_admin_reaches_annotator(stack, require):
    require("admin")
    require("annotator")
    r = stack.probe("admin", [{"name": "annotator", "type": "http",
                               "url": "${ANNOTATOR_URL:-http://annotator:8010}/health", "expect": [200]}])["annotator"]
    assert r["ok"], r["detail"]
