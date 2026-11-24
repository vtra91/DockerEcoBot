import logging
from fastapi import APIRouter
from pydantic import BaseModel, Field
from search_api.services.llm_answer_generator import LLMAnswerGenerator

logger = logging.getLogger(__name__)
router = APIRouter()

_FALLBACK = "Привет! Я Экоассистент Байкальского музея. Спросите о флоре, фауне или местах Байкала."


class SmalltalkRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)


class SmalltalkResponse(BaseModel):
    answer: str


@router.post("/smalltalk", response_model=SmalltalkResponse)
async def smalltalk(request: SmalltalkRequest):
    logger.info(f"Smalltalk endpoint called: {request.query[:80]!r}")
    try:
        generator = LLMAnswerGenerator(vector_search=None)
        result = generator.generate_smalltalk(request.query)
        answer = (result.get("content") or "").strip() or _FALLBACK
        return {"answer": answer}
    except Exception as e:
        logger.error(f"Smalltalk endpoint error: {e}", exc_info=True)
        return {"answer": _FALLBACK}