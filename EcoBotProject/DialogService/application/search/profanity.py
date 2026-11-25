"""Фильтр нецензурной лексики для входящих сообщений бота.

За основу взята библиотека profanity-filter (словарь + pymorphy2), поэтому
свой список матов в репозитории не хранится. Фильтр подгружается лениво и
один раз на процесс: core-api создаёт оркестратор на каждый HTTP-запрос, и
повторная загрузка словарей на каждое сообщение недопустима.

Любая ошибка фильтра трактуется как «не брань»: слой модерации не должен
ломать основной сценарий ответа на запрос.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_filter = None          # ленивый синглтон ProfanityFilter
_filter_failed = False  # чтобы не пытаться загрузить его на каждом сообщении


def enabled() -> bool:
    """PROFANITY_FILTER_ENABLED=0/false/no/off — слой брани выключен."""
    raw = os.getenv("PROFANITY_FILTER_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _get_filter():
    global _filter, _filter_failed
    if _filter is not None or _filter_failed:
        return _filter
    try:
        from profanity_filter import ProfanityFilter

        _filter = ProfanityFilter()
        logger.info("ProfanityFilter загружен — нецензурная лексика будет отсекаться")
    except Exception as exc:  # библиотека/словари недоступны
        _filter_failed = True
        logger.warning("ProfanityFilter недоступен, слой брани отключён: %s", exc)
    return _filter


def warmup() -> None:
    """Предзагрузка словарей при старте, чтобы первое сообщение не ждало."""
    if enabled():
        _get_filter()


def is_abusive(text: str) -> bool:
    """True, если в тексте есть нецензурная лексика."""
    if not text or not text.strip() or not enabled():
        return False

    pf = _get_filter()
    if pf is None:
        return False

    try:
        if hasattr(pf, "is_profane"):
            return bool(pf.is_profane(text))
        # Совместимость с версиями библиотеки без is_profane():
        # censored-версия отличается от исходного текста, только если что-то нашли.
        return pf.censor(text) != text
    except Exception as exc:
        logger.debug("ProfanityFilter error: %s", exc)
        return False
