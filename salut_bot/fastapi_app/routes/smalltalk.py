import logging

from fastapi import APIRouter
from pydantic import BaseModel, Field

from search_api.services.faq_service import FAQService
from search_api.services.llm_answer_generator import LLMAnswerGenerator

logger = logging.getLogger(__name__)
router = APIRouter()

_FALLBACK = "Я — Экоассистент Байкальского музея. Спросите о флоре, фауне или местах Байкала."


class SmalltalkRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)


class SmalltalkResponse(BaseModel):
    answer: str
    source: str = "llm"  # faq | llm | fallback — для логов и отладки


@router.post("/smalltalk", response_model=SmalltalkResponse)
async def smalltalk(request: SmalltalkRequest):
    logger.info(f"Smalltalk endpoint called: {request.query[:80]!r}")

    # 1. Справочник: ответ детерминирован, LLM не вызываем.
    try:
        hit = FAQService.get().find(request.query)
        if hit:
            logger.info("Smalltalk FAQ hit: id=%s query=%r", hit.get("id"), request.query[:80])
            return {"answer": hit["answer"], "source": "faq"}
    except Exception as exc:
        logger.warning("FAQ lookup failed, fallback to LLM: %s", exc)

    # 2. Свободная болтовня — генерирует LLM.
    try:
        generator = LLMAnswerGenerator(vector_search=None)
        result = generator.generate_smalltalk(request.query)
        answer = (result.get("content") or "").strip()
        if answer:
            return {"answer": answer, "source": "llm"}
    except Exception as exc:
        logger.error(f"Smalltalk endpoint error: {exc}", exc_info=True)

    return {"answer": _FALLBACK, "source": "fallback"}
