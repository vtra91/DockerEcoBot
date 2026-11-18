# Новый курсор "Покажи ещё"
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, fields
from typing import Any

logger = logging.getLogger(__name__)

_MORE_KEY_PREFIX = "more:"
_DEFAULT_TTL = 900  # 15 минут - столько же, сколько живёт история диалога


@dataclass
class ContinuationCursor:
    """Куда продолжать прошлый ответ."""

    kind: str                      # images / text / objects / place
    query: str                     # исходный запрос пользователя
    slots: dict[str, Any]          # слоты исходного запроса
    object_offset: int = 0         # с какой страницы объектов продолжать
    object_limit: int = 20         # размер страницы объектов
    object_has_more: bool = False  # есть ли ещё страницы объектов
    resource_offset: int = 0       # сколько ресурсов (картинок/текстов) уже отдано
    shown: int = 0                 # сколько уже показано/использовано
    total: int = 0                 # сколько всего доступно
    created_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "ContinuationCursor | None":
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None
            allowed = {f.name for f in fields(cls)}
            return cls(**{k: v for k, v in data.items() if k in allowed})
        except Exception as e:
            logger.warning(f"ContinuationCursor.from_json: {e}")
            return None


class ContinuationStore:
    """Хранит курсор "Покажи ещё" в Redis (ключ more:{user_id})"""

    def __init__(self, redis_client, ttl: int = _DEFAULT_TTL):
        self._redis = redis_client
        self._ttl = ttl

    def _key(self, user_id: str) -> str:
        return f"{_MORE_KEY_PREFIX}{user_id}"

    async def get(self, user_id: str) -> ContinuationCursor | None:
        if not self._redis or not user_id:
            return None
        try:
            raw = await self._redis.get(self._key(user_id))
            if not raw:
                return None
            return ContinuationCursor.from_json(raw)
        except Exception as e:
            logger.error(f"ContinuationStore.get [{user_id}]: {e}")
            return None

    async def save(self, user_id: str, cursor: ContinuationCursor) -> None:
        if not self._redis or not user_id:
            return
        try:
            if not cursor.created_at:
                cursor.created_at = time.time()
            await self._redis.set(self._key(user_id), cursor.to_json(), ex=self._ttl)
        except Exception as e:
            logger.error(f"ContinuationStore.save [{user_id}]: {e}")

    async def clear(self, user_id: str) -> None:
        if not self._redis or not user_id:
            return
        try:
            await self._redis.delete(self._key(user_id))
        except Exception as e:
            logger.error(f"ContinuationStore.clear [{user_id}]: {e}")
