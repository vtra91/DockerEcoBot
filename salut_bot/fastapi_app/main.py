import os
import shutil
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi_app.routes import all_routers

logger = logging.getLogger(__name__)

FAQ_PATH = Path(os.getenv("FAQ_PATH", "/app/faq/faq.json"))
FAQ_DEFAULTS_PATH = Path("/app/faq_defaults/faq.json")


def _seed_faq() -> None:
    """Заполняет постоянный FAQ заводским значением, если файла ещё нет.

    /app/faq примонтирован как volume (data/faq), при первом старте он пуст.
    Копируем дефолт, не перезаписывая уже существующий файл.
    """
    try:
        if FAQ_PATH.exists() or not FAQ_DEFAULTS_PATH.exists():
            return
        FAQ_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FAQ_DEFAULTS_PATH, FAQ_PATH)
    except Exception as exc:
        logger.warning("FAQ seeding failed: %s", exc)


_seed_faq()

BASE_PATH = os.getenv("BASE_PATH", "/ecoassistant").rstrip("/")

app = FastAPI(
    title="EcoAssistant Search API (Salut Bot)",
    root_path=BASE_PATH,
)

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in all_routers:
    app.include_router(r)


@app.get("/health")
async def health_check():
    return {"status": "ok", "base_path": BASE_PATH}
