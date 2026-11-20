import os

import pytest
import requests

from stack import CORE_SERVICES, OPTIONAL_SERVICES, Stack, internal_url, normalize_base_path

TIMEOUT = float(os.getenv("HEALTH_TIMEOUT", "15"))
SLOW_TIMEOUT = float(os.getenv("HEALTH_SLOW_TIMEOUT", "240"))


def pytest_configure(config):
    for name, text in [
        ("slow", "долгие проверки (LLM, распознавание речи); пропустить: -m 'not slow'"),
        ("llm", "нужен работающий LLM (Ollama/GigaChat)"),
        ("public", "через публичный адрес PUBLIC_BASE_URL (HEALTH_SKIP_PUBLIC=1 — пропустить)"),
        ("host", "через системный nginx хоста (HEALTH_SKIP_HOST=1 — пропустить)"),
        ("security", "то, что не должно быть доступно без авторизации"),
    ]:
        config.addinivalue_line("markers", "%s: %s" % (name, text))


def pytest_collection_modifyitems(config, items):
    skip_public = os.getenv("HEALTH_SKIP_PUBLIC") == "1"
    skip_host = os.getenv("HEALTH_SKIP_HOST") == "1"
    for item in items:
        if skip_public and "public" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="HEALTH_SKIP_PUBLIC=1"))
        if skip_host and "host" in item.keywords:
            item.add_marker(pytest.mark.skip(reason="HEALTH_SKIP_HOST=1"))


@pytest.fixture(scope="session")
def stack():
    return Stack()


@pytest.fixture(scope="session")
def require(stack):
    """require('annotator') — пропустить тест, если необязательный сервис не запущен;
    для основного сервиса — упасть с понятной причиной."""
    def _require(service):
        if stack.is_running(service):
            return
        if service in OPTIONAL_SERVICES or service not in CORE_SERVICES:
            pytest.skip("%s не запущен (сервис под профилем)" % service)
        pytest.fail("%s не запущен — смотрите test_01_containers и `docker compose logs %s`" % (service, service))
    return _require


@pytest.fixture(scope="session")
def base_path(stack):
    return normalize_base_path(stack.env("backend").get("BASE_PATH"))


@pytest.fixture(scope="session")
def public_base_url(stack):
    url = (os.getenv("HEALTH_PUBLIC_BASE_URL") or stack.env("backend").get("PUBLIC_BASE_URL") or "").rstrip("/")
    if not url:
        pytest.skip("PUBLIC_BASE_URL не задан")
    return url


class Http:
    """requests с базовым адресом и таймаутом по умолчанию, без автопереходов по редиректам."""

    def __init__(self, base):
        self.base = base.rstrip("/")
        self.session = requests.Session()

    def request(self, method, path, **kw):
        kw.setdefault("timeout", TIMEOUT)
        kw.setdefault("allow_redirects", False)
        url = path if path.startswith("http") else self.base + path
        return self.session.request(method, url, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)


@pytest.fixture(scope="session")
def http():
    """http('backend').get('/health')"""
    cache = {}

    def _http(service_or_url):
        if service_or_url not in cache:
            base = service_or_url if "://" in service_or_url else internal_url(service_or_url)
            cache[service_or_url] = Http(base)
        return cache[service_or_url]
    return _http


@pytest.fixture(scope="session")
def db_params(stack):
    """Параметры БД ровно те, с которыми работает backend (с теми же значениями по умолчанию, что в коде)."""
    env = stack.env("backend")
    if not env:
        pytest.fail("backend не найден — не знаю, к какой БД подключаться")
    return {
        "host": env.get("DB_HOST", "db"),
        "port": int(env.get("DB_PORT", "5432")),
        "dbname": env.get("DB_NAME", "eco"),
        "user": env.get("DB_USER", "postgres"),
        "password": env.get("DB_PASSWORD", ""),
    }


@pytest.fixture(scope="session")
def db(db_params):
    import psycopg2
    try:
        conn = psycopg2.connect(connect_timeout=int(TIMEOUT), **db_params)
    except Exception as exc:
        pytest.fail("Не подключиться к БД с параметрами backend (%s@%s/%s): %s"
                    % (db_params["user"], db_params["host"], db_params["dbname"], exc))
    conn.set_session(readonly=True, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def sql(db):
    def _sql(query, params=None):
        with db.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()
    return _sql


@pytest.fixture(scope="session")
def redis_env(stack):
    env = stack.env("backend") or stack.env("admin")
    return {"host": env.get("REDIS_HOST", "redis"), "port": int(env.get("REDIS_PORT", "6379")),
            "password": env.get("REDIS_PASSWORD") or None}


@pytest.fixture(scope="session")
def redis_db(redis_env):
    import redis

    clients = {}

    def _redis(db_index=0):
        if db_index not in clients:
            clients[db_index] = redis.Redis(db=db_index, decode_responses=True,
                                            socket_timeout=TIMEOUT, **redis_env)
        return clients[db_index]
    return _redis


@pytest.fixture(scope="session")
def admin_session(stack, http, base_path):
    """Залогиненная сессия админки. Логин/пароль: HEALTH_ADMIN_USER/HEALTH_ADMIN_PASSWORD,
    иначе ADMIN_USERNAME/ADMIN_PASSWORD из окружения контейнера admin."""
    env = stack.env("admin")
    user = os.getenv("HEALTH_ADMIN_USER") or env.get("ADMIN_USERNAME", "admin")
    password = os.getenv("HEALTH_ADMIN_PASSWORD") or env.get("ADMIN_PASSWORD")
    if not password:
        pytest.skip("Пароль админки неизвестен: задайте HEALTH_ADMIN_PASSWORD")
    client = http("admin")
    resp = client.post("/login", data={"username": user, "password": password})
    cookie_name = "ecoassistant_admin_session"
    if resp.status_code != 303 or cookie_name not in resp.cookies:
        pytest.fail("Логин в админку как %r не удался (HTTP %s, Location=%s). Если пароль меняли в UI — "
                    "передайте его через HEALTH_ADMIN_PASSWORD."
                    % (user, resp.status_code, resp.headers.get("location")))
    # Cookie выдана на путь {BASE_PATH}/admin, а мы ходим в admin напрямую по "/",
    # поэтому передаём её заголовком, а не через cookie-jar.
    return {"Cookie": "%s=%s" % (cookie_name, resp.cookies[cookie_name])}


def fmt_failures(results):
    return "\n".join("  ✗ %s: %s" % (r["name"], r["detail"]) for r in results if not r["ok"])
