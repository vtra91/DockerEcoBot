"""Семантический роутер: решает, куда отдать сообщение пользователя.

Три режима обработки:
    SEARCH    — обычный поиск по базе знаний (/search);
    SMALLTALK — болтовня, приветствия, вопросы о самом боте (FAQ + LLM);
    ABUSE     — оскорбления/брань, которые не поймал детерминированный фильтр.

Роутер не генерирует ответ и не читает FAQ — только выбирает маршрут, поэтому
его промпт остаётся коротким, а FAQ можно пополнять независимо от него.
При любой ошибке LLM возвращается безопасный маршрут SEARCH.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

from infrastructure.llm.factory import LLMFactory

logger = logging.getLogger("SemanticRouter")

RouteMode = Literal["SEARCH", "SMALLTALK", "ABUSE"]

ROUTER_PROMPT = """Ты — маршрутизатор сообщений эко-бота о Байкальском регионе.
Определи, как обрабатывать сообщение пользователя.

Категории:
• SEARCH — пользователь хочет найти или узнать данные из базы знаний: растения, животные, места, достопримечательности, музеи, заповедники, услуги (билеты, режим работы, как добраться), фотографии, карты, списки объектов.
• SMALLTALK — это НЕ запрос о данных: приветствия, благодарности, прощания, эмоции, вопросы о самом боте (кто тебя создал, как тебя зовут, что ты умеешь, откуда берёшь информацию, что это за бот), пустая болтовня («как дела», «расскажи шутку»).
• ABUSE — оскорбления, нецензурная лексика, угрозы, агрессия в адрес бота или людей.

ПРАВИЛА:
1. Сомневаешься между SEARCH и SMALLTALK — выбирай SEARCH.
2. Короткий вопрос про объект/место/услугу — это SEARCH, даже без глагола: «нерпа?», «музеи Иркутск», «билеты».
3. Вопрос о самом боте (создатели, имя, возможности, источники данных) — это SMALLTALK.
4. ABUSE — только при явной грубости; нейтральное упоминание слова грубостью не считается.
5. Продолжение предыдущего поискового диалога («а где он обитает?», «покажи ещё») — это SEARCH.
{context}
СООБЩЕНИЕ: {query}

Ответь строго JSON: {{"mode": "SEARCH"}} или {{"mode": "SMALLTALK"}} или {{"mode": "ABUSE"}}
"""


class RouteResult(BaseModel):
    mode: RouteMode = "SEARCH"


class SemanticRouter:
    def __init__(self, provider: str = "qwen"):
        self._provider = provider
        llm = LLMFactory.get_model(provider)
        self.parser = llm.with_structured_output(RouteResult, method="json_mode")

    def reload(self) -> None:
        import os

        provider = os.getenv("LLM_PROVIDER", self._provider)
        llm = LLMFactory.get_model(provider)
        self.parser = llm.with_structured_output(RouteResult, method="json_mode")
        self._provider = provider

    async def route(self, query: str, prev_query: Optional[str] = None) -> str:
        context = ""
        if prev_query:
            context = (
                f"\nКОНТЕКСТ: предыдущий запрос пользователя — «{prev_query}». "
                "Если текущее сообщение продолжает его (местоимения «он/она/там», "
                "«а ещё», «покажи ещё») — это SEARCH.\n"
            )

        prompt = ROUTER_PROMPT.format(context=context, query=query)
        try:
            result: RouteResult = await self.parser.ainvoke(prompt)
            mode = result.mode
            if mode not in ("SEARCH", "SMALLTALK", "ABUSE"):
                return "SEARCH"
            logger.info("🧭 Route=%s query=%r", mode, query[:60])
            return mode
        except Exception as exc:
            logger.warning("SemanticRouter error, fallback=SEARCH: %s", exc)
            return "SEARCH"


_router_cache: dict[str, SemanticRouter] = {}


def get_router(provider: str = "qwen") -> SemanticRouter:
    """Кеширует роутер на процесс: core-api создаёт оркестратор на каждый запрос."""
    router = _router_cache.get(provider)
    if router is None:
        router = SemanticRouter(provider=provider)
        _router_cache[provider] = router
    return router
