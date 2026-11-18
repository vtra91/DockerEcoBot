from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from typing import Any

from application.search.context_manager import ConversationHistory, DialogueTurn
from application.search.slot_classifier import SlotClassifier, _determine_template, _detect_ambiguity
from application.search.slot_search_executor import SlotSearchExecutor

#from application.search.ordinal_resolver import parse_ordinal, OUT_OF_RANGE_TEMPLATE
from utils.error_logger import log_nlu_miss

logger = logging.getLogger(__name__)

def _ms(t: float) -> int:
    return round((time.monotonic() - t) * 1000)


_SIMPLIFY_KEY_PREFIX = "simplify:"
_SIMPLIFY_TTL = 300  # 5 минут — пока пользователь видит кнопки

# Маркеры явной отмены фильтров: "без сезона", "убери фильтр", "вообще все" и т.п.
_FEATURE_CLEAR_RE = re.compile(
    r"\bбез\b|\bубери\b|\bудали\b|\bотмени\b|\bне нужно\b|\bвообще\b",
    re.IGNORECASE,
)

_STOP_WORDS = {
    "а", "и", "но", "да", "нет", "ну", "вот", "же", "ли", "бы", "уже", "ещё", "еще",
    "мне", "тебе", "вам", "нас", "вас", "это", "этот", "эта", "эти", "то", "так",
    "очень", "просто", "всё", "все", "хорошо", "ок", "окей", "ладно",
}
_SEARCH_MARKERS = {
    "покажи", "покажите", "показать", "найди", "найдите", "найти",
    "расскажи", "расскажите", "рассказать", "подскажи", "подскажите",
    "где", "когда", "почему", "сколько", "какой", "какая", "какие", "каких",
    "режим", "расписание", "билет", "билеты", "цена", "стоимость",
    "контакт", "контакты", "телефон", "адрес", "как добраться",
    "карта", "карте", "фото", "фотография",
}




# ── Контекстный мердж ─────────────────────────────────────────────────────────

def _merge_with_context(new_slots: dict, prev: DialogueTurn, query: str) -> tuple[dict, bool]:
    """
    Детерминированно заполняет пустые слоты из предыдущего turn'а.
    Наследование происходит только когда в новом запросе нет нового объекта
    и тип объекта совпадает (или не определён).
    Возвращает (merged_slots, is_continuation).
    """
    prev_slots = prev.slots
    merged = dict(new_slots)

    new_synonym = merged.get("synonym")
    new_obj_type = merged.get("object_type")
    prev_synonym = prev_slots.get("synonym")
    prev_obj_type = prev_slots.get("object_type")

    new_props = new_slots.get("properties") or {}
    new_extra = new_slots.get("extra") or {}
    new_features = new_slots.get("features") or {}
    new_has_filters = bool(new_props) or bool(new_extra) or bool(new_features)

    can_inherit = (
            new_synonym is None
            and not new_has_filters
            and (new_obj_type is None or new_obj_type == prev_obj_type)
    )

    if not can_inherit:
        logger.info(
            f"[CTX] Наследование пропущено: "
            f"new_synonym={new_synonym!r} new_obj_type={new_obj_type!r} "
            f"prev_obj_type={prev_obj_type!r} → новая тема"
        )
        return merged, False

    inherited: list[str] = []

    if prev_synonym:
        merged["synonym"] = prev_synonym
        inherited.append(f"synonym={prev_synonym!r}")
    if prev_obj_type and not new_obj_type:
        merged["object_type"] = prev_obj_type
        inherited.append(f"object_type={prev_obj_type!r}")

    # Features: наследуем если запрос не содержит явной отмены фильтров
    prev_features = dict(prev_slots.get("features") or {})
    new_features = dict(merged.get("features") or {})
    if prev_features and _should_clear_features(query):
        # Пользователь явно просит убрать фильтры — берём только новые
        merged["features"] = new_features
        inherited.append(f"features=СБРОШЕНЫ (запрос содержит отмену)")
    else:
        merged["features"] = {**prev_features, **new_features}
        if prev_features:
            inherited.append(f"features: {prev_features} + {new_features} = {merged['features']}")

    prev_props = dict(prev_slots.get("properties") or {})
    new_props = dict(merged.get("properties") or {})
    merged["properties"] = {**prev_props, **new_props}

    prev_extra = dict(prev_slots.get("extra") or {})
    new_extra = dict(merged.get("extra") or {})
    merged["extra"] = {**prev_extra, **new_extra}

    logger.info(f"[CTX] Контекст применён: {', '.join(inherited) if inherited else 'ничего нового'}")
    return merged, True


# ── Проактивность ─────────────────────────────────────────────────────────────

def _build_proactive(slots: dict, result: dict, has_db_content: bool = True) -> dict[str, str]:
    """
    Определяет что предложить после ответа.
    Не делает доп. запросов — основывается на типе найденных данных и модальности.
    """
    entity = slots.get("synonym")
    if not entity or not has_db_content:
        return {}

    modality = slots.get("modality", "Текст")
    object_type = slots.get("object_type")
    is_off = object_type == "Объект флоры и фауны"
    is_geo = object_type == "Географический объект"
    # Фото уместно только для живых существ и географических мест, но не для услуг
    can_have_photo = is_off or is_geo
    suggest: dict[str, str] = {}

    if modality == "Текст" and result.get("answer"):
        if can_have_photo:
            suggest["photo"] = entity
        if is_off:
            suggest["map"] = entity

    elif modality == "Изображение" and result.get("images"):
        if is_off:
            suggest["map"] = entity
        suggest["text"] = entity

    elif modality == "Геоданные" and (result.get("map") or result.get("objects")):
        suggest["text"] = entity

    return suggest


# ── Вспомогательные ───────────────────────────────────────────────────────────

def _resolve_promo_ref(slots: dict, prev_promo: list) -> dict:
    """
    Если synonym из нового запроса является подстрокой имени промо-объекта
    (или наоборот) — подставляем db_id и тип промо-объекта для точного поиска.
    """
    synonym = (slots.get("synonym") or "").lower().strip()
    if not synonym or not prev_promo:
        return slots
    for item in prev_promo:
        name = (item.get("name") or "").lower()
        if not name:
            continue
        if synonym in name or name in synonym:
            logger.info(f"[PROMO] Resolved promo ref: synonym={synonym!r} → db_id={item['db_id']!r}")
            slots = dict(slots)
            slots["promo_db_id"] = item["db_id"]
            slots["promo_name"] = item.get("name", "")
            slots["synonym"] = None
            slots["object_type"] = item.get("object_type")
            return slots
    return slots


def _has_content(result: dict) -> bool:
    return bool(
        result.get("answer")
        or result.get("images")
        or result.get("objects")
        or result.get("map")
    )


def _truncate_entity(name: str, max_len: int = 50) -> str:
    return name[:max_len] if name else name


def _attach_db_ids(items: list[str], objects: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    lowered = [
        (str(o.get("name") or "").strip().lower(), str(o.get("db_id") or ""))
        for o in (objects or [])
        if o.get("name") and o.get("db_id")
    ]
    for text in items:
        item_text = (text or "").strip()
        item_low = item_text.lower()
        db_id = ""
        for name, obj_db_id in lowered:
            if len(name) >= 4 and name in item_low:
                db_id = obj_db_id
                break
        enriched.append({"text": item_text, "db_id": db_id})
    return enriched


def _items_from_objects(answer: str, objects: list[dict]) -> list[dict]:
    text_low = (answer or "").lower()
    if not text_low:
        return []

    found: list[tuple[int, str, str]] = []
    for obj in objects or []:
        name = str(obj.get("name") or "").strip()
        db_id = str(obj.get("db_id") or "")

        if len(name) < 4:
            continue
        pos = text_low.find(name.lower())
        if pos < 0:
            continue
        found.append((pos, name, db_id))

    if len(found) < 2:
        return []

    found.sort(key=lambda x: x[0])
    return [{"text": name, "db_id": db_id} for _, name, db_id in found]



def _slots_from_list_item(item: dict, modality: str, snapshot: dict) -> dict:
    text = (item.get("text") or "").strip()
    db_id = item.get("db_id") or ""
    object_type = snapshot.get("object_type") or "Объект флоры и фауны"

    slots: dict = {
        "object_type": object_type,
        "synonym": None,
        "properties": {},
        "features": {},
        "extra": {},
        "modality": modality,
        "template": None,
        "modality_ambiguous": False,
        "from_list": True,
        "list_item_text": text,
    }

    if db_id:
        slots["promo_db_id"] = db_id
        slots["promo_name"] = text
    else:

        name = text.split(":")[0].strip()
        name = re.sub(r"^\d+[.)]\s*", "", name)
        slots["synonym"] = name[:100] if name else None

    return slots


# ── Оркестратор ───────────────────────────────────────────────────────────────

class DialogueOrchestrator:
    def __init__(
        self,
        classifier: SlotClassifier,
        executor: SlotSearchExecutor,
        history: ConversationHistory,
        redis_client=None,
    ):
        self._classifier = classifier
        self._executor = executor
        self._history = history
        self._redis = redis_client


    async def process(self, query: str, user_id: str | None = None, promo_enabled: bool | None = None,
                      multi_act_enabled: bool | None = None) -> dict:
        """Полный цикл: классификация → мердж контекста → поиск → проактивность."""
        t0 = time.monotonic()


        multi_act = multi_act_enabled if multi_act_enabled is not None else True
        turns = await self._history.get_turns(user_id) if (user_id and multi_act) else []
        prev = turns[-1] if turns else None

        # Передаём предыдущий запрос и промо в LLM — для разрешения местоимений
        prev_promo = prev.promo_objects if prev else []
        t_classify = time.monotonic()
        slots = await self._classifier.classify(query, prev_query=prev.query if prev else None, prev_promo=prev_promo)
        classify_ms = _ms(t_classify)

        if slots.get("error") and user_id and self._executor._session:
            await log_nlu_miss(
                self._executor._session, query, user_id,
                reason=f"LLM classify error: {slots['error']}",
            )

        is_continuation = False
        context_ms = 0
        if prev:
            t_ctx = time.monotonic()
            slots, is_continuation = _merge_with_context(slots, prev, query)
            slots["template"] = _determine_template(slots)
            slots["modality_ambiguous"] = _detect_ambiguity(query, slots["modality"])
            context_ms = _ms(t_ctx)

        # Резолюция ссылок на промо-объекты (после мёрджа контекста)
        if prev_promo:
            slots = _resolve_promo_ref(slots, prev_promo)

        logger.info(
            f"[CTX] Итог [{user_id}]: "
            f"synonym={slots.get('synonym')!r} "
            f"object_type={slots.get('object_type')!r} "
            f"modality={slots.get('modality')!r} "
            f"features={slots.get('features')} "
            f"continuation={is_continuation}"
        )

        result = await self._execute_and_finalize(query, slots, user_id, is_continuation,
                                                  promo_enabled=promo_enabled,
                                                  prev_query=prev.query if prev else None,
                                                  multi_act=multi_act)
        exec_timing = result.get("timing", {})
        result["timing"] = {
            "classify_ms": classify_ms,
            "context_ms": context_ms,
            **exec_timing,
            "total_ms": _ms(t0),
        }
        return result

    async def process_with_slots(
        self, query: str, slots: dict, user_id: str | None = None
    ) -> dict:
        """Выполнение с готовыми слотами — для callback упрощений."""
        t0 = time.monotonic()
        result = await self._execute_and_finalize(query, slots, user_id, is_continuation=False)
        exec_timing = result.get("timing", {})
        result["timing"] = {**exec_timing, "total_ms": _ms(t0)}
        return result

    async def _execute_and_finalize(
        self,
        query: str,
        slots: dict,
        user_id: str | None,
        is_continuation: bool,
        promo_enabled: bool | None = None,
        prev_query: str | None = None,
        multi_act: bool = True,
    ) -> dict:
        t_search = time.monotonic()
        log_context = {"current_query": query, "prev_query": prev_query}
        pipeline_result = await self._executor.execute(query, slots, user_id=user_id, promo_enabled=promo_enabled, context=log_context)
        search_ms = _ms(t_search)
        result = pipeline_result.get("result", {})

        # Сценарий 4: нет результатов → ищем упрощения параллельно
        simplifications: list[dict] = []
        simplifications_ms = 0
        if user_id and self._redis and not _has_content(result):
            t_simp = time.monotonic()
            simplifications = await self._try_simplifications(query, slots, user_id)
            simplifications_ms = _ms(t_simp)

        search_data = pipeline_result.get("search") or {}
        has_db_content = bool(
            search_data.get("objects") or
            search_data.get("resources") or
            result.get("images") or
            result.get("map")
        )
        proactive = _build_proactive(slots, result, has_db_content)

        if user_id and multi_act:
            await self._history.add_turn(user_id, DialogueTurn(
                query=query,
                slots=slots,
                had_results=_has_content(result),
                promo_objects=result.get("promo") or [],
            ))


        pipeline_result["proactive"] = proactive
        pipeline_result["modality_ambiguous"] = slots.get("modality_ambiguous", False)
        pipeline_result["is_continuation"] = is_continuation
        pipeline_result["simplifications"] = simplifications
        pipeline_result["timing"] = {
            "search_ms": search_ms,
            "simplifications_ms": simplifications_ms,
        }

        return pipeline_result

    async def _try_simplifications(
        self, query: str, slots: dict, user_id: str
    ) -> list[dict]:
        """
        Сценарий 4: параллельная проверка упрощённых запросов.
        Снимаем features по одному и проверяем есть ли результаты.
        """
        features = dict(slots.get("features") or {})
        if not features:
            return []

        candidates: list[tuple[str, dict]] = []
        for key in features:
            s = {**slots, "features": {k: v for k, v in features.items() if k != key}}
            candidates.append((f"Без фильтра «{key}»", s))

        if len(features) > 1:
            candidates.append(("Без всех фильтров", {**slots, "features": {}}))

        async def _check(label: str, s_slots: dict) -> dict | None:
            try:
                r = await self._executor.execute(query, s_slots, user_id=None)
                if _has_content(r.get("result", {})):
                    return {"label": label, "query": query, "slots": s_slots}
            except Exception:
                pass
            return None

        results = await asyncio.gather(*[_check(l, s) for l, s in candidates])
        found = [r for r in results if r]

        if found:
            key = f"{_SIMPLIFY_KEY_PREFIX}{user_id}"
            await self._redis.set(
                key,
                json.dumps(found, ensure_ascii=False),
                ex=_SIMPLIFY_TTL,
            )

        return found

    async def load_simplification(self, user_id: str, idx: int) -> dict | None:
        """Загружает сохранённый вариант упрощения по индексу."""
        if not self._redis:
            return None
        try:
            raw = await self._redis.get(f"{_SIMPLIFY_KEY_PREFIX}{user_id}")
            if not raw:
                return None
            items = json.loads(raw)
            return items[idx] if 0 <= idx < len(items) else None
        except Exception as e:
            logger.error(f"load_simplification [{user_id}]: {e}")
            return None
