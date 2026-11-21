""" POST /reload — горячее применение LLM-конфига в контейнере backend."""

import logging
import os

from fastapi import APIRouter

from infrastructure.config_reload import SHARED_ENV_PATH, apply_shared_env
from infrastructure import llm_integration as infra_llm
from search_api.services import llm_integration as search_llm

logger = logging.getLogger(__name__)
router = APIRouter()


def _reset_service_singletons() -> dict:
    """ Сбрасывает LLM у сервисов-синглтонов backend."""
    result = {"search_service": False, "relational_service": False}
    try:
        from fastapi_app.dependencies import search_service, relational_service
    except Exception as exc:
        logger.warning("[Reload] Не удалось импортировать синглтоны: %s", exc)
        return result

    if hasattr(search_service, "reload_llm"):
        search_service.reload_llm()
        result["search_service"] = True
    if hasattr(relational_service, "reload_llm"):
        relational_service.reload_llm()
        result["relational_service"] = True
    return result


@router.post("/reload")
async def reload_config():
    """Применяет новый LLM-конфиг без пересоздания контейнера."""
    report = apply_shared_env(SHARED_ENV_PATH)

    dropped_infra = infra_llm.reset_llm_cache()
    dropped_search = search_llm.reset_llm_cache()
    services = _reset_service_singletons()

    payload = {
        "status": "error" if report["error"] else "success",
        "message": report["error"] or "Конфиг LLM применён",
        "report": report,
        "dropped_instances": {
            "infrastructure": dropped_infra,
            "search_api": dropped_search,
        },
        "services_reloaded": services,
    }

    if report["error"]:
        logger.error("[Reload] %s", report["error"])
    else:
        logger.info(
            "[Reload] LLM-конфиг применён. Провайдер=%s, выброшено клиентов: %s",
            os.getenv("LLM_PROVIDER", "?"),
            dropped_infra + dropped_search,
        )

    return payload
