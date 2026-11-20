"""Уровень 1: итоговое окружение контейнеров согласовано между сервисами.

Читаем не .env-файлы, а то, что реально попало в контейнер (env_file + environment),
поэтому тест ловит и «забыл пересоздать контейнер после правки shared.env».
"""
import re
import socket
import urllib.parse

import pytest

from stack import OPTIONAL_SERVICES, env_list, normalize_base_path

# Сервисы под профилями: если не запущены, ссылки на них в env не считаем ошибкой
PROFILE_SERVICES = set(OPTIONAL_SERVICES) | {"annotator", "images-extractor"}

DB_SERVICES = ["backend", "core-api", "admin"]          # сервисы, которые ходят в Postgres
BASE_PATH_SERVICES = ["backend", "core-api", "max", "admin"]


@pytest.mark.parametrize("service", DB_SERVICES)
def test_db_credentials_match_postgres(stack, require, service):
    """DB_NAME/DB_USER/DB_PASSWORD сервиса совпадают с тем, чем инициализирован контейнер db.

    Внимание: POSTGRES_* применяются только при ПЕРВОЙ инициализации data/postgres_data.
    Поэтому реальную авторизацию проверяет test_03 (подключение изнутри сервиса)."""
    require(service)
    db = stack.env("db")
    env = stack.env(service)
    expected = {"DB_NAME": db.get("POSTGRES_DB", "postgres"),
                "DB_USER": db.get("POSTGRES_USER", "postgres"),
                "DB_PASSWORD": db.get("POSTGRES_PASSWORD")}
    defaults = {"DB_NAME": "eco", "DB_USER": "postgres", "DB_PASSWORD": None}   # как в коде сервисов
    diff = []
    for key, want in expected.items():
        got = env.get(key, defaults[key])
        if got != want:
            shown = (lambda v: "***" if v else repr(v)) if key == "DB_PASSWORD" else repr
            diff.append("%s: у %s %s, у db %s" % (key, service, shown(got), shown(want)))
    assert not diff, "Параметры БД расходятся:\n  " + "\n  ".join(diff)


def test_base_path_consistent(stack):
    values = {s: stack.env(s).get("BASE_PATH") for s in BASE_PATH_SERVICES if stack.env(s)}
    missing = [s for s, v in values.items() if v is None]
    assert not missing, (
        "BASE_PATH не задан явно в %s. Значения по умолчанию в коде разные ('' и '/ecoassistant' — "
        "даже внутри AdminPanel/main.py два разных), так что без явного значения ссылки разъедутся. "
        "Задайте BASE_PATH в shared.env." % missing)
    normalized = {s: normalize_base_path(v) for s, v in values.items()}
    assert len(set(normalized.values())) == 1, "BASE_PATH различается: %s" % normalized


def test_public_base_url_matches_base_path(stack):
    env = stack.env("backend")
    url = env.get("PUBLIC_BASE_URL", "")
    base = normalize_base_path(env.get("BASE_PATH"))
    assert url, "PUBLIC_BASE_URL не задан — ссылки на карты/фото в ответах бота будут битые"
    path = urllib.parse.urlparse(url).path.rstrip("/")
    assert path == base, "PUBLIC_BASE_URL=%s оканчивается на %r, а BASE_PATH=%r" % (url, path, base)


@pytest.mark.parametrize("service", ["core-api", "max"])
def test_dialogservice_config_imports(stack, require, service):
    """DialogService/config.py падает при импорте, если нет хоть одной обязательной переменной
    (get_env_var без default бросает ValueError — в том числе для PLANTNET_API_KEY с default=None)."""
    require(service)
    code, out = stack.exec(service, ["python", "-c", "import config"])
    assert code == 0, "%s: `import config` падает — сервис не стартует или падает на первом запросе:\n%s" % (
        service, out.strip().splitlines()[-1] if out.strip() else out)


def test_max_webhook_settings(stack, require):
    require("max")
    env = stack.env("max")
    assert env.get("MAX_BOT_TOKEN"), "MAX_BOT_TOKEN не задан — бот не стартует"
    if env.get("MAX_USE_WEBHOOK", "").lower() == "true":
        url = env.get("MAX_WEBHOOK_URL") or "%s/%s" % (env.get("PUBLIC_BASE_URL", "").rstrip("/"),
                                                      env.get("MAX_WEBHOOK_PATH", "/").lstrip("/"))
        assert url.startswith("https://"), (
            "Webhook MAX = %r, но MAX принимает только https. В main_max.py при не-https адресе подписка "
            "молча пропускается — бот просто не получает сообщений." % url)


SECRET_KEYS = ["DB_PASSWORD", "POSTGRES_PASSWORD", "SESSION_SECRET_KEY", "SECRET_KEY", "ADMIN_PASSWORD",
               "API_AUTH_TOKEN", "TEST_TOKEN", "ANNOTATOR_TOKEN", "ANNOTATOR_API_TOKEN", "STAND_SECRET_KEY",
               "MAX_WEBHOOK_SECRET", "MAX_BOT_TOKEN", "BOT_TOKEN"]
PLACEHOLDER = re.compile(
    r"^(changeme|change-me|change_me|postgres|admin|password|secret|test)$"
    r"|your_|replace-with|generate-a-random|same-as-|super-secret|change-me$|changeme$", re.I)


def test_no_placeholder_secrets(stack):
    ignore = set(env_list("HEALTH_IGNORE_ENV"))
    bad = {}
    for service in stack.services_running():
        for key, value in stack.env(service).items():
            if key in SECRET_KEYS and key not in ignore and value and PLACEHOLDER.search(value):
                bad.setdefault((key, value), []).append(service)
    lines = ["%s=%r (в %s)" % (k, v, ", ".join(s)) for (k, v), s in sorted(bad.items())]
    assert not lines, ("Секреты со значениями из .env.example (сервер виден из сети университета). "
                       "Если переменная никем не читается — удалите её из env:\n  " + "\n  ".join(lines))


def test_admin_tokens_match_peers(stack, require):
    require("admin")
    admin = stack.env("admin")
    problems = []
    dsapi = stack.env("dsapi")
    if dsapi and admin.get("TEST_TOKEN") != dsapi.get("API_AUTH_TOKEN"):
        problems.append("admin TEST_TOKEN ≠ dsapi API_AUTH_TOKEN — тестирование из админки получит 401")
    if stack.is_running("annotator"):
        ann = stack.env("annotator").get("ANNOTATOR_API_TOKEN") or ""
        if ann and admin.get("ANNOTATOR_TOKEN", "") != ann:
            problems.append("admin ANNOTATOR_TOKEN ≠ annotator ANNOTATOR_API_TOKEN — аннотатор ответит 401")
    assert not problems, "\n".join(problems)


_URL = re.compile(r"^(https?)://([A-Za-z0-9_-]+)(?::(\d+))?(/.*)?$")


def test_internal_urls_in_env_point_to_live_services(stack):
    """Любой http://<имя-без-точки>:порт в окружении любого сервиса должен вести в живой сервис сети.

    Ловит мусор вроде SALUT_BOT_URL=http://salut_bot:5000 или BOT_DOMAIN=http://backend:5555:
    такие переменные либо сломаны, либо не используются — и то и другое надо убрать."""
    ignore = set(env_list("HEALTH_IGNORE_ENV"))
    status = {}                       # (host, port) -> None | текст ошибки
    bad = {}                          # (key, value) -> [сервисы]
    for service in stack.services_running():
        if service == "health-tests":
            continue
        for key, value in stack.env(service).items():
            m = _URL.match(value.strip())
            if not m or key in ignore:
                continue
            scheme, host, port, _ = m.groups()
            if host == "localhost" or (host in PROFILE_SERVICES and not stack.is_running(host)):
                continue
            target = (host, int(port or (443 if scheme == "https" else 80)))
            if target not in status:
                try:
                    socket.create_connection(target, timeout=3).close()
                    status[target] = None
                except OSError as exc:
                    status[target] = "%s: %s" % (exc.__class__.__name__, exc)
            if status[target]:
                bad.setdefault((key, value, status[target]), []).append(service)
    lines = ["%s=%s (в %s) → %s" % (k, v, ", ".join(s), err) for (k, v, err), s in sorted(bad.items())]
    assert not lines, ("Переменные указывают на несуществующие адреса (исключить: HEALTH_IGNORE_ENV=VAR1,VAR2):\n  "
                       + "\n  ".join(lines))
