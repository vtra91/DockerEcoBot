"""Уровень 0: контейнеры живы, не перезапускаются по кругу, в логах с момента старта нет падений."""
import os
import re

import pytest

from stack import CORE_SERVICES, OPTIONAL_SERVICES

ALL = CORE_SERVICES + [s for s in OPTIONAL_SERVICES if s not in CORE_SERVICES]
MAX_RESTARTS = int(os.getenv("HEALTH_MAX_RESTARTS", "0"))

# Что считаем признаком поломки в логах (с момента последнего старта контейнера)
LOG_PATTERNS = [
    r"Traceback \(most recent call last\)",
    r"\b(ImportError|ModuleNotFoundError|SyntaxError)\b",
    r"\bCRITICAL\b",
    r"Application startup failed",
    r"Exception in ASGI application",
    r"\[emerg\]",                                    # nginx
    r"FATAL: +(password authentication failed|database .* does not exist|role .* does not exist)",  # postgres
    r"Cannot load valid features from DB",           # core-api тихо отключает валидацию признаков
]
LOG_IGNORE = os.getenv("HEALTH_LOG_IGNORE")  # регулярка для известного шума


def _state(stack, service):
    c = stack.container(service)
    if c is None:
        if service in OPTIONAL_SERVICES:
            pytest.skip("%s не создан (профиль не включён)" % service)
        pytest.fail("Контейнер %s не найден в проекте %s. Запустите: docker compose up -d %s"
                    % (service, stack.project, service))
    return c, c.attrs["State"]


@pytest.mark.parametrize("service", ALL)
def test_container_running(stack, service):
    c, state = _state(stack, service)
    assert state["Status"] == "running", (
        "%s: статус %s, exit code %s, ошибка %r. Последние строки лога:\n%s"
        % (service, state["Status"], state.get("ExitCode"), state.get("Error"),
           c.logs(tail=30).decode("utf-8", "replace")))


@pytest.mark.parametrize("service", ALL)
def test_container_healthy(stack, service):
    c, state = _state(stack, service)
    health = state.get("Health")
    if not health:
        pytest.skip("у %s нет healthcheck в compose" % service)
    last = (health.get("Log") or [{}])[-1]
    assert health["Status"] == "healthy", (
        "%s: healthcheck = %s, последний вывод: %s" % (service, health["Status"], (last.get("Output") or "").strip()))


@pytest.mark.parametrize("service", ALL)
def test_no_restart_loop(stack, service):
    """restart: always маскирует падения: контейнер 'running', но каждые N секунд перезапускается."""
    c, state = _state(stack, service)
    restarts = c.attrs.get("RestartCount", 0)
    assert restarts <= MAX_RESTARTS, (
        "%s перезапускался %d раз(а) с момента создания — значит, падает. Смотрите "
        "`docker compose logs --tail=100 %s`" % (service, restarts, service))


@pytest.mark.parametrize("service", ALL)
def test_logs_have_no_errors(stack, service):
    c, state = _state(stack, service)
    if state["Status"] != "running":
        pytest.skip("контейнер не запущен — см. test_container_running")
    lines = stack.logs_since_start(service).splitlines()
    hits = []
    for i, line in enumerate(lines):
        if LOG_IGNORE and re.search(LOG_IGNORE, line):
            continue
        if any(re.search(p, line) for p in LOG_PATTERNS):
            hits.append("\n".join(lines[max(0, i - 2): i + 4]))
    assert not hits, (
        "%s: в логах с момента старта %d подозрительных мест. Первые:\n\n%s\n\n"
        "(известный шум можно исключить регуляркой HEALTH_LOG_IGNORE)"
        % (service, len(hits), "\n----\n".join(hits[:3])))
