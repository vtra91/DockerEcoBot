"""То, что не должно быть доступно без авторизации.

Проверяем через docker-nginx (маршрутизация та же, что у публичного адреса), поэтому тесты работают
и без доступа в интернет. Только безопасные запросы: GET и POST /reload_database с reload_database=false
(при false эндпоинт сразу возвращает ошибку и ничего не делает).
"""
import os

import pytest

pytestmark = pytest.mark.security

LOGIN_REDIRECT = (302, 303, 307, 401, 403)


def _upstream_alive(resp, path):
    if resp.status_code in (502, 503, 504):
        pytest.fail("%s → HTTP %s: сервис за nginx не отвечает, проверить безопасность нельзя (см. test_01)"
                    % (path, resp.status_code))


@pytest.mark.parametrize("path", [
    "/admin/",
    "/admin/logs",                         # журнал ошибок с запросами пользователей
    "/admin/logs/stats",
    "/admin/object/search?q=%D0%BD%D0%B5%D1%80%D0%BF%D0%B0",   # поиск по БД объектов
    "/admin/object/link-types",
    "/admin/chat/promo-setting",
])
def test_admin_requires_login(http, require, path):
    require("nginx")
    resp = http("nginx").get(path)
    _upstream_alive(resp, path)
    assert resp.status_code in LOGIN_REDIRECT, (
        "%s без логина → HTTP %s (%d байт). Проверка сессии в AdminPanel/main.py сделана вручную в каждом "
        "роуте, и здесь её нет." % (path, resp.status_code, len(resp.content)))


@pytest.mark.parametrize("path", ["/core-api/prompts", "/core-api/config"])
def test_core_api_admin_endpoints_not_public(http, require, path):
    """У core-api нет авторизации, а nginx публикует его целиком: POST /core-api/prompts и /core-api/config
    позволяют кому угодно из интернета переписать промпты LLM и .env бота."""
    require("nginx")
    resp = http("nginx").get(path)
    _upstream_alive(resp, path)
    assert resp.status_code in (401, 403, 404), "%s доступен без авторизации: HTTP %s" % (path, resp.status_code)


def test_backend_reload_database_not_public(http, require):
    require("nginx")
    resp = http("nginx").post("/reload_database", data={"reload_database": "false"})
    _upstream_alive(resp, "/reload_database")
    assert resp.status_code in (401, 403, 404), (
        "POST /reload_database (перезагрузка всей БД) доступен из интернета без авторизации: HTTP %s %s"
        % (resp.status_code, resp.text[:120]))


def test_admin_default_password_rejected(http, require):
    require("admin")
    resp = http("admin").post("/login", data={"username": "admin", "password": "admin"})
    assert "ecoassistant_admin_session" not in resp.cookies, "В админку пускает admin/admin"


def test_no_ports_published_to_network(stack):
    """Фаервол на сервере выключен: всё, что опубликовано на 0.0.0.0, видно из сети университета.
    На cloud-b наружу не должно смотреть ничего (nginx — только 127.0.0.1:8088)."""
    allowed = {int(p) for p in os.getenv("HEALTH_ALLOWED_PUBLIC_PORTS", "").split(",") if p.strip()}
    exposed = []
    for service in stack.services_running():
        ports = stack.container(service).attrs["NetworkSettings"].get("Ports") or {}
        for container_port, bindings in ports.items():
            for b in bindings or []:
                if b.get("HostIp") in ("0.0.0.0", "::", "") and int(b["HostPort"]) not in allowed:
                    exposed.append("%s: %s:%s → %s" % (service, b.get("HostIp") or "*", b["HostPort"], container_port))
    assert not exposed, ("Опубликовано на все интерфейсы (на cloud-b должен быть подключён "
                         "docker-compose.cloudb.yml через COMPOSE_FILE в .env):\n  " + "\n  ".join(sorted(set(exposed))))
