"""Уровень 6: каждый сервис отвечает по своему API (напрямую по ecobot_net, без nginx)
и контракты между сервисами совпадают: то, что вызывает клиент, реально есть у сервера."""
import io
import json
import math
import struct
import wave

import pytest

from conftest import SLOW_TIMEOUT

# Маршруты backend, на которые завязаны core-api/max (DialogService/config.py API_URLS) и dsapi
BACKEND_ROUTES = [
    "/health", "/search", "/species/description", "/object/description", "/search_images_by_features",
    "/get_coords", "/coords_to_map", "/objects_in_polygon_simply", "/find_species_with_description",
    "/objects_in_area_by_type", "/log_error", "/faiss_status", "/search/place/objects", "/objects/related",
    "/find_off_near_attractions", "/reload_database",
]
CORE_API_ROUTES = ["/classify", "/search_pipeline", "/callback_simplify", "/prompts", "/config", "/reload"]


def openapi_paths(http, service):
    resp = http(service).get("/openapi.json")
    assert resp.status_code == 200, "%s: /openapi.json → HTTP %s" % (service, resp.status_code)
    return set(resp.json()["paths"])


# --- backend -----------------------------------------------------------------

def test_backend_health(http, require):
    require("backend")
    resp = http("backend").get("/health")
    assert resp.status_code == 200 and resp.json().get("status") == "ok", resp.text[:300]


def test_backend_all_routes_registered(http, require):
    """Ловит регресс вида `app.include_router(router)` вместо цикла по all_routers."""
    require("backend")
    missing = [p for p in BACKEND_ROUTES if p not in openapi_paths(http, "backend")]
    assert not missing, "backend не зарегистрировал маршруты: %s" % missing


def test_backend_faiss_loaded(http, require):
    require("backend")
    data = http("backend").get("/faiss_status").json()
    assert data.get("status") == "success", data
    info = data["data"]
    assert info.get("faiss_vectorstore_loaded"), "FAISS не загружен: %s" % info
    assert info.get("index_size", 0) > 0, "FAISS-индекс пустой: %s" % info


def test_backend_search_returns_data(http, require):
    """POST /search уходит в Postgres; ошибки backend возвращает как HTTP 200 + {"error": ...}."""
    require("backend")
    body = {"system_parameters": {"limit": 5},
            "search_parameters": {"modality_type": "Текст",
                                  "object": {"name_synonyms": {"ru_names": ["Байкальская нерпа"]}}}}
    resp = http("backend").post("/search", json=body, timeout=60)
    assert resp.status_code == 200, resp.text[:300]
    data = resp.json()
    assert "error" not in data, "backend /search: %s" % data["error"]
    assert data.get("resources") or data.get("objects"), "Пустой ответ /search для нерпы: %s" % str(data)[:300]


def test_backend_species_description(http, require):
    require("backend")
    resp = http("backend").get("/species/description", params={"species_name": "Байкальская нерпа"}, timeout=120)
    assert resp.status_code == 200, "HTTP %s: %s" % (resp.status_code, resp.text[:300])
    assert resp.json().get("count", 0) > 0, resp.text[:300]


def test_dialogservice_urls_exist_in_backend(stack, http, require):
    """Контракт core-api → backend: каждый адрес из DialogService/config.py API_URLS есть в backend."""
    require("core-api")
    require("backend")
    code, out = stack.exec("core-api", ["python", "-c", "import config, json; print(json.dumps(config.API_URLS))"])
    if code != 0:
        pytest.fail("config.py в core-api не импортируется: %s" % out.strip().splitlines()[-1])
    urls = json.loads(out.strip().splitlines()[-1])
    backend_base = stack.env("core-api").get("ECOBOT_API_BASE_URL", "").rstrip("/")
    paths = openapi_paths(http, "backend")
    missing = []
    for name, url in urls.items():
        if not url.startswith(backend_base):
            continue   # stand_endpoint — внешний сервис
        path = url[len(backend_base):].rstrip("/") or "/"
        if path not in paths:
            missing.append("%s → %s" % (name, url))
    assert not missing, "core-api вызывает несуществующие маршруты backend:\n  " + "\n  ".join(missing)


def test_backend_trailing_slash_redirect_works(http, require):
    """API_URLS['get_description'] = .../species/description/ (со слэшем). FastAPI отвечает 307 на адрес
    без слэша — Location должен вести обратно в backend, а не на адрес с BASE_PATH, которого внутри нет."""
    require("backend")
    resp = http("backend").get("/species/description/", params={"species_name": "Байкальская нерпа"},
                               allow_redirects=True, timeout=120)
    assert resp.status_code == 200, "HTTP %s после редиректов %s" % (
        resp.status_code, [r.headers.get("location") for r in resp.history])


# --- core-api ----------------------------------------------------------------

def test_core_api_routes(http, require):
    require("core-api")
    missing = [p for p in CORE_API_ROUTES if p not in openapi_paths(http, "core-api")]
    assert not missing, "core-api: нет маршрутов %s (их вызывает админка)" % missing


def test_core_api_prompts(http, require):
    require("core-api")
    prompts = http("core-api").get("/prompts").json()
    assert prompts.get("classification_prompt.txt"), "core-api не видит prompts/classification_prompt.txt"


@pytest.mark.slow
@pytest.mark.llm
def test_core_api_classify(http, require):
    """core-api → LLM: классификация запроса на слоты."""
    require("core-api")
    resp = http("core-api").post("/classify", json={"query": "Расскажи о байкальской нерпе"}, timeout=SLOW_TIMEOUT)
    assert resp.status_code == 200, resp.text[:300]
    data = resp.json()
    # ошибку LLM core-api не пробрасывает, а кладёт в поле error ответа и отвечает 200
    assert isinstance(data, dict) and not data.get("error"), "LLM вернул ошибку: %s" % data.get("error")


# --- admin -------------------------------------------------------------------

def test_admin_login_page(http, require):
    require("admin")
    resp = http("admin").get("/login")
    assert resp.status_code == 200 and 'name="password"' in resp.text, "HTTP %s" % resp.status_code


def test_admin_redirects_to_login_with_base_path(http, require, base_path):
    require("admin")
    resp = http("admin").get("/")
    assert resp.status_code in (302, 303, 307), "HTTP %s" % resp.status_code
    location = resp.headers.get("location", "")
    assert location == "%s/admin/login" % base_path, "Location=%r, ожидали %r" % (location, base_path + "/admin/login")


def test_admin_static(http, require):
    require("admin")
    resp = http("admin").get("/admin/static/css/style.css")
    assert resp.status_code == 200 and "css" in resp.headers.get("content-type", ""), resp.status_code


@pytest.mark.parametrize("page", ["/", "/flora", "/geographical", "/settings", "/properties"])
def test_admin_pages_after_login(http, require, admin_session, page):
    """Страницы админки ходят в Postgres (asyncpg) и core-api (/settings → /prompts)."""
    require("admin")
    resp = http("admin").get(page, headers=admin_session, timeout=60)
    assert resp.status_code == 200, "%s → HTTP %s: %s" % (page, resp.status_code, resp.text[:300])
    assert "Internal Server Error" not in resp.text


def test_admin_bot_status_api(http, require):
    require("admin")
    data = http("admin").get("/bot-status").json()
    assert "online" in data, data


# --- dsapi -------------------------------------------------------------------

def test_dsapi_up(http, require):
    require("dsapi")
    assert "/api/v1/pipeline_testing/start" in openapi_paths(http, "dsapi")


def test_dsapi_accepts_admin_token(stack, http, require):
    """Контракт admin → dsapi: TEST_API_BASE + Bearer TEST_TOKEN."""
    require("dsapi")
    require("admin")
    env = stack.env("admin")
    base, token = env.get("TEST_API_BASE", ""), env.get("TEST_TOKEN", "")
    assert base, "TEST_API_BASE в admin не задан"
    resp = http(base).get("/status/healthcheck-nonexistent", headers={"Authorization": "Bearer " + token})
    assert resp.status_code not in (401, 403), "dsapi отверг токен админки: HTTP %s" % resp.status_code
    assert resp.status_code in (200, 404), "HTTP %s: %s" % (resp.status_code, resp.text[:200])


# --- stt-service -------------------------------------------------------------

def test_stt_health(http, require):
    require("stt-service")
    data = http("stt-service").get("/health").json()
    assert data.get("status") == "ok", data


def _tone_wav(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(seconds * rate))))
    return buf.getvalue()


@pytest.mark.slow
def test_stt_transcribe(http, require):
    """ffmpeg + whisper реально работают. Первый вызов скачивает модель (нужен интернет к HuggingFace)."""
    require("stt-service")
    resp = http("stt-service").post("/transcribe", files={"file": ("tone.wav", _tone_wav(), "audio/wav")},
                                    timeout=SLOW_TIMEOUT * 2)
    assert resp.status_code == 200, "HTTP %s: %s" % (resp.status_code, resp.text[:300])
    assert "text" in resp.json()


# --- max -----------------------------------------------------------------------

def test_max_webhook_listening(stack, http, require):
    """aiohttp-сервер maxapi слушает MAX_WEBHOOK_PORT/MAX_WEBHOOK_PATH (только в режиме webhook)."""
    require("max")
    env = stack.env("max")
    if env.get("MAX_USE_WEBHOOK", "").lower() != "true":
        pytest.skip("max в режиме polling")
    path = "/" + env.get("MAX_WEBHOOK_PATH", "/").lstrip("/")
    resp = http("max").get(path)
    assert resp.status_code != 404, "max не обслуживает %s (HTTP 404)" % path


# --- annotator ---------------------------------------------------------------

def test_annotator_health(http, require):
    require("annotator")
    data = http("annotator").get("/health").json()
    assert data.get("status") == "ok", data
    assert data.get("executors"), "Ни одна модель аннотатора не загрузилась: %s" % data
    assert not data.get("failed"), "Модели аннотатора с ошибкой загрузки: %s" % data["failed"]


def test_annotator_accepts_admin_token(stack, http, require):
    require("annotator")
    require("admin")
    token = stack.env("admin").get("ANNOTATOR_TOKEN", "")
    resp = http("annotator").get("/executors", headers={"X-API-Token": token})
    assert resp.status_code == 200, "аннотатор отверг токен админки: HTTP %s" % resp.status_code
