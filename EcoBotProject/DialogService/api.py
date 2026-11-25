import os
import asyncio
import logging
import aiohttp
import redis.asyncio as aioredis
from contextlib import asynccontextmanager
from fastapi import FastAPI

from adapters.http.routes.search import router as search_router
from adapters.http.routes.config import router as config_router
from application.search.profanity import warmup as profanity_warmup
from infrastructure.db_feature_loader import load_valid_features

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.session = aiohttp.ClientSession()
    app.state.valid_features = await load_valid_features()
    app.state.redis = aioredis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=0,
        decode_responses=True,
    )
    # Предзагрузка фильтра брани: словари pymorphy2 тяжёлые, грузим один раз.
    try:
        await asyncio.to_thread(profanity_warmup)
    except Exception as exc:
        logger.warning("Profanity warmup failed: %s", exc)
    logger.info("Core API: сессия открыта.")
    yield
    await app.state.session.close()
    await app.state.redis.aclose()
    logger.info("Core API: сессия закрыта.")


# BASE_PATH (shared.env) + /core-api — публичный адрес этого сервиса за nginx.
# Нужен только для /docs и openapi.json, маршруты не меняются.
_BASE_PATH = os.getenv("BASE_PATH", "").strip().strip("/")
_ROOT_PATH = (f"/{_BASE_PATH}" if _BASE_PATH else "") + "/core-api"

app = FastAPI(title="EcoBot Core API", lifespan=lifespan, root_path=_ROOT_PATH)
app.include_router(search_router)
app.include_router(config_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5001)
