"""Уровень 7: маршрутизация снаружи внутрь.

  cloud-b (срезает /ecoassistant) → системный nginx хоста :80 → docker-nginx → сервисы

Внутренний docker-nginx проверяем запросами без префикса — ровно так, как их присылает cloud-b.
Затем ту же цепочку через nginx хоста и через публичный адрес.
"""
import random
import urllib.parse

import pytest

ROUTES = [
    # (путь от корня после cloud-b, ожидаемый статус, кусок ответа / заголовок, кто должен ответить)
    ("/health", 200, '"status"', "backend"),
    ("/search/health", 200, '"status"', "backend через /search/"),
    ("/core-api/openapi.json", 200, '"/search_pipeline"', "core-api"),
    ("/admin/login", 200, 'name="password"', "admin"),
    ("/admin/static/css/style.css", 200, None, "admin (статика)"),
]


@pytest.mark.parametrize("path,status,needle,who", ROUTES, ids=[r[0] for r in ROUTES])
def test_nginx_routes(http, require, path, status, needle, who):
    require("nginx")
    resp = http("nginx").get(path)
    assert resp.status_code == status, "%s → HTTP %s (должен отвечать %s)" % (path, resp.status_code, who)
    if needle:
        assert needle in resp.text, "%s ответил не %s: %s" % (path, who, resp.text[:200])


def test_nginx_admin_redirect_keeps_single_prefix(http, require, base_path):
    require("nginx")
    resp = http("nginx").get("/admin/")
    location = resp.headers.get("location", "")
    assert resp.status_code in (302, 303, 307), resp.status_code
    assert location.endswith("%s/admin/login" % base_path) and "/ecoassistant/ecoassistant" not in location, location


def test_nginx_routes_max_webhook_to_max(stack, http, require):
    """README и docker-compose.cloudb.yml обещают /ecoassistant/max-webhook → max:8080.
    Без отдельного location запрос падает в `location /` → backend → 404, и бот не получает сообщений."""
    require("nginx")
    require("max")
    env = stack.env("max")
    if env.get("MAX_USE_WEBHOOK", "").lower() != "true":
        pytest.skip("max в режиме polling")
    path = "/" + env.get("MAX_WEBHOOK_PATH", "/max-webhook").lstrip("/")
    via_nginx = http("nginx").get(path)
    direct = http("max").get(path)
    assert via_nginx.status_code == direct.status_code, (
        "GET %s: через nginx HTTP %s (%s), напрямую в max HTTP %s — nginx отправляет webhook не в max"
        % (path, via_nginx.status_code, via_nginx.text[:80], direct.status_code))


def _sample_image_urls(sql, n=5):
    rows = sql("select file_path from eco_assistant.image_value where file_path ~ '^https?://' "
               "order by random() limit %s", (n,))
    return [r[0] for r in rows]


def test_images_from_db_are_served(sql, http, require, public_base_url):
    """Фото, на которые ссылается БД, реально отдаются. Локальные (PUBLIC_BASE_URL/images/...) — через
    docker-nginx; внешние (например IMPORT_IMAGES_BASE_URL=https://testecobot.ru) — по их адресу."""
    require("nginx")
    urls = _sample_image_urls(sql)
    if not urls:
        pytest.skip("в БД нет картинок с URL")
    broken = []
    for url in urls:
        if url.startswith(public_base_url + "/"):
            target = http("nginx").base + urllib.parse.quote(url[len(public_base_url):])
        else:
            target = url
        try:
            resp = http("nginx").get(target, timeout=20, allow_redirects=True)
            ok = resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/")
            if not ok:
                broken.append("%s → HTTP %s %s" % (url, resp.status_code, resp.headers.get("content-type")))
        except Exception as exc:
            broken.append("%s → %s" % (url, exc.__class__.__name__))
    assert not broken, "Из %d случайных фото не открываются:\n  %s" % (len(urls), "\n  ".join(broken))


def test_maps_are_served(stack, http, require):
    require("nginx")
    code, out = stack.exec("nginx", ["sh", "-c", "ls /var/www/maps | grep -E '\\.(html|png)$' | head -50"])
    names = [x for x in out.splitlines() if x.strip()]
    if not names:
        pytest.skip("в maps/ пока нет файлов")
    name = random.choice(names)
    resp = http("nginx").get("/maps/" + urllib.parse.quote(name))
    assert resp.status_code == 200, "/maps/%s → HTTP %s" % (name, resp.status_code)


@pytest.mark.host
@pytest.mark.parametrize("path,status,needle,who", ROUTES[:4], ids=[r[0] for r in ROUTES[:4]])
def test_host_nginx_chain(http, path, status, needle, who):
    """Системный nginx хоста (/etc/nginx/sites-enabled.d/ecoassistant.conf) → 127.0.0.1:8088."""
    resp = http("http://host.docker.internal:80").get(path)
    assert resp.status_code == status, "хост :80 %s → HTTP %s (должен отвечать %s)" % (path, resp.status_code, who)
    if needle:
        assert needle in resp.text


@pytest.mark.public
@pytest.mark.parametrize("path,status,needle,who", ROUTES[:4], ids=[r[0] for r in ROUTES[:4]])
def test_public_url(http, public_base_url, path, status, needle, who):
    resp = http(public_base_url).get(path, timeout=30)
    assert resp.status_code == status, "%s%s → HTTP %s" % (public_base_url, path, resp.status_code)
    if needle:
        assert needle in resp.text


@pytest.mark.public
def test_public_admin_redirect(http, public_base_url, base_path):
    resp = http(public_base_url).get("/admin/", timeout=30)
    location = resp.headers.get("location", "")
    assert resp.status_code in (302, 303, 307), resp.status_code
    assert urllib.parse.urlparse(location).path == base_path + "/admin/login", location
