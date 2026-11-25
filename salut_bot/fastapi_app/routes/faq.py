"""GET/POST /faq — справочник smalltalk-ответов, редактируется из админки.

GET  отдаёт текущий список записей;
POST принимает {"items": [...]} (или сразу список), валидирует и сохраняет
файл FAQ, после чего FAQService сразу видит новую версию.
"""

import logging

from fastapi import APIRouter, Body, HTTPException

from search_api.services.faq_service import FAQService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/faq")
async def get_faq():
    return {"items": FAQService.get().items()}


@router.post("/faq")
async def update_faq(data=Body(...)):
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Ожидается список записей FAQ")
    try:
        count = FAQService.get().save(items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("FAQ save error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить FAQ: {exc}")
    return {"status": "success", "count": count}
