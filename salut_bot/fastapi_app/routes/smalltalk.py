import logging
import re

from fastapi import APIRouter
from pydantic import BaseModel, Field

from search_api.services.faq_service import FAQService
from search_api.services.llm_answer_generator import LLMAnswerGenerator

logger = logging.getLogger(__name__)
router = APIRouter()

_FALLBACK = "Я — Экоассистент Байкальского музея. Спросите о флоре, фауне или местах Байкала."

# На «чистые» эмоции отвечаем детерминированно: LLM на короткую реплику без
# контекста иногда выдумывает странные уточняющие вопросы.
_EMOTION_REPLIES = {
    "прикольно": [
        "Ага, и правда забавно! На Байкальской природной территории такие сюрпризы встречаются на каждом шагу.",
        "Согласен, забавно! Байкал умеет удивлять.",
    ],
    "класс": [
        "Рад, что понравилось! Байкальская природная территория умеет удивлять.",
        "Класс! Тогда расскажу ещё что-нибудь интересное о Байкальской природной территории.",
    ],
    "классно": ["Рад, что понравилось! Байкал умеет удивлять."],
    "круто": ["Согласен, круто! В Байкале действительно есть чему удивиться."],
    "ого": ["Вот именно! Байкал полон неожиданностей."],
    "вау": ["Вот именно! Байкал полон неожиданностей."],
    "супер": ["Рад, что понравилось! Байкал умеет удивлять."],
    "здорово": ["Рад, что понравилось! Байкал умеет удивлять."],
    "интересно": ["И правда интересно! В природе Байкала много неожиданного."],
    "любопытно": ["И правда любопытно! Байкал умеет удивлять."],
}


def _emotion_reply(query: str) -> str | None:
    """Ответ на одиночное эмоциональное слово; иначе None."""
    normalized = re.sub(r"[^\w\s]+", "", query.lower().replace("ё", "е")).strip()
    words = normalized.split()
    if len(words) != 1:
        return None
    options = _EMOTION_REPLIES.get(words[0])
    if not options:
        return None
    return options[sum(ord(ch) for ch in query) % len(options)]


class SmalltalkRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)


class SmalltalkResponse(BaseModel):
    answer: str
    source: str = "llm"  # faq | llm | fallback — для логов и отладки
    faq: dict | None = None  # запись FAQ, если ответ взят из справочника


@router.post("/smalltalk", response_model=SmalltalkResponse)
async def smalltalk(request: SmalltalkRequest):
    logger.info(f"Smalltalk endpoint called: {request.query[:80]!r}")

    # 1. Справочник: ответ детерминирован, LLM не вызываем.
    try:
        hit = FAQService.get().find(request.query)
        if hit:
            logger.info("Smalltalk FAQ hit: id=%s query=%r", hit.get("id"), request.query[:80])
            return {"answer": hit["answer"], "source": "faq", "faq": hit}
    except Exception as exc:
        logger.warning("FAQ lookup failed, fallback to LLM: %s", exc)

    # 1.5. Чистая эмоция — заготовленный ответ (без LLM).
    emotion = _emotion_reply(request.query)
    if emotion:
        logger.info("Smalltalk emotion reply: %r", request.query[:40])
        return {"answer": emotion, "source": "emotion"}

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
