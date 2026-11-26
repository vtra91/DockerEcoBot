import os
import re
import logging
import aiohttp
from typing import Any, Dict, List, Optional

from config import API_URLS, STAND_SECRET_KEY
from utils.stand_manager import is_stand_session_active
from utils.error_logger import log_api_fail, log_zero_results
from utils.bot_messages import (
    IMAGE_NOT_FOUND_OBJECTS,
    IMAGE_NOT_FOUND_SYNONYM,
    IMAGE_NOT_FOUND_GENERIC,
    IMAGE_SUGGESTIONS,
)


class _ApiError(Exception):
    def __init__(self, status: int, url: str, response_text: str, payload=None):
        self.status = status
        self.url = url
        self.response_text = response_text
        self.payload = payload
        super().__init__(f"API {status} at {url}")

_NEAR_RE = re.compile(r"рядом|около|возле|недалеко|поблизости|близ", re.IGNORECASE)

IMAGES_PER_ANSWER = 3
IMAGE_PAGE_SIZE = IMAGES_PER_ANSWER
TEXT_PAGE_SIZE = 1
OBJECT_PAGE_SIZE = 20

_LOCATION_KEYS = frozenset({"Детальное расположение", "Расположение относительно Байкала"})


def _normalize_map_links(links) -> dict:
    """Приводит map_links из API к единому формату {"static": url, "interactive": url}."""
    if isinstance(links, str):
        return {"static": links, "interactive": None}
    if isinstance(links, dict):
        static = (
            links.get("static")
            or links.get("static_map")
            or links.get("static_url")
        )
        interactive = (
            links.get("interactive")
            or links.get("interactive_map")
            or links.get("interactive_url")
        )
        return {"static": static, "interactive": interactive}
    return {}
_MD_IMG_RE = re.compile(r'!\[[^\]]*\]\([^)]*\)')           # ![alt](url) → удалить
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]+\)')          # [text](url) → text
_EXCESS_NL_RE = re.compile(r'\n{3,}')

logger = logging.getLogger(__name__)


class SlotSearchExecutor:
    """Исполнительная часть пайплайна нового классификатора.
    Преобразует слоты в параметры запроса к /search или /place/objects
    и возвращает результат.
    """

    def __init__(self, session: aiohttp.ClientSession = None):
        self._session = session
        self._backend_url = os.getenv("ECOBOT_API_BASE_URL", "http://backend:5555")
        self._promo_enabled = os.getenv("PROMO_ENABLED", "false").lower() == "true"
        self._promo_relation_type = os.getenv("PROMO_RELATION_TYPE", "promo")

    async def execute(
        self,
        query: str,
        slots: Dict[str, Any],
        user_id: str | None = None,
        promo_enabled: bool | None = None,
        context: Optional[Dict[str, Any]] = None,
        *,
        object_offset: int = 0,
        object_limit: int | None = None,
        resource_offset: int = 0,
        resource_limit: int | None = None,
        use_llm_answer: bool | None = None,
        include_resources: bool = True,
        similar_objects: bool = True,
    ) -> Dict[str, Any]:
        try:
            if self._use_place_endpoint(slots):
                search_body = self._build_place_params(
                    slots, query, offset=object_offset, limit=object_limit
                )
                search_data = await self._call_place(search_body)
            else:
                search_body = self._build_search_params(
                    slots,
                    query,
                    object_offset=object_offset,
                    object_limit=object_limit,
                    resource_offset=resource_offset,
                    resource_limit=resource_limit,
                    use_llm_answer=use_llm_answer,
                    include_resources=include_resources,
                )
                search_data = await self._call_search(search_body)
        except _ApiError as e:
            if user_id and self._session:
                await log_api_fail(
                    self._session, user_id, e.url, e.status, e.response_text, query,
                    context=context, payload=e.payload,
                )
            return {"slots": slots, "search_request": {}, "search": {}, "result": {"answer": "Сервис временно недоступен. Попробуйте позже.", "error": True}}

        if user_id and is_stand_session_active(user_id):
            await self._send_to_stand(search_data)

        promo_objects = []
        use_promo = promo_enabled if promo_enabled is not None else self._promo_enabled
        if use_promo and not slots.get("promo_db_id"):
            promo_objects = await self._fetch_promo(search_data.get("objects") or [], query)

        result = self._format_result(
            slots,
            search_data,
            promo_objects,
            object_offset=object_offset,
            resource_offset=resource_offset,
        )

        if result.get("no_images") and similar_objects:
            await self._attach_similar_objects(result, slots)

        if user_id and self._session and self._is_zero_result(slots, result, search_data):
            await log_zero_results(
                self._session, query, user_id,
                action=slots.get("template") or slots.get("modality", "unknown"),
                search_params=search_body,
                context=context,
            )

        return {"slots": slots, "search_request": search_body, "search": search_data, "result": result}

    async def match_faq(self, query: str) -> dict | None:
        """Детерминированный поиск по FAQ — вызывается до роутера."""
        own_session = self._session is None
        sess = self._session or aiohttp.ClientSession()
        url = f"{self._backend_url}/faq/match"
        try:
            async with sess.post(url, json={"query": query}, timeout=5) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    return data.get("match")
        except Exception as e:
            logger.warning(f"FAQ match failed: {e}")
        finally:
            if own_session:
                await sess.close()
        return None

    async def respond_smalltalk(self, query: str) -> str:
        """POST /smalltalk на backend; возвращает готовый текст или '' при ошибке."""
        details = await self.respond_smalltalk_details(query)
        return details.get("answer") or ""

    async def respond_smalltalk_details(self, query: str) -> dict:
        """POST /smalltalk; возвращает {"answer", "source", "faq"} — для ответа и debug-панели."""
        own_session = self._session is None
        sess = self._session or aiohttp.ClientSession()
        url = f"{self._backend_url}/smalltalk"
        try:
            async with sess.post(url, json={"query": query}, timeout=20) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning(f"Smalltalk HTTP {resp.status}: {text[:200]}")
                    return {}
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return {}
                return {
                    "answer": (data.get("answer") or "").strip(),
                    "source": data.get("source"),
                    "faq": data.get("faq"),
                }
        except Exception as e:
            logger.error(f"Smalltalk call failed: {e}")
            return {}
        finally:
            if own_session:
                await sess.close()

    async def _attach_similar_objects(self, result: dict, slots: Dict[str, Any]) -> None:
        """Добавляет к ответу похожие объекты, у которых точно есть фото."""
        synonym = (slots.get("synonym") or "").strip()
        if not synonym:
            return

        objects = result.get("objects") or []
        exclude_db_id = objects[0].get("db_id") if objects else None
        items = await self._fetch_similar_objects(
            synonym, slots.get("object_type"), exclude_db_id
        )
        if not items:
            return

        suggestions = [
            {"name": item.get("name"), "db_id": item.get("db_id")}
            for item in items
            if item.get("name")
        ]
        if not suggestions:
            return

        result["suggestions"] = suggestions
        names = ", ".join(s["name"] for s in suggestions)
        base = (result.get("answer") or "").strip()
        hint = IMAGE_SUGGESTIONS.replace("{names}", names)
        result["answer"] = f"{base} {hint}".strip()

    async def _fetch_similar_objects(
        self, synonym: str, object_type: str | None = None, exclude_db_id: str | None = None
    ) -> List[dict]:
        own_session = self._session is None
        sess = self._session or aiohttp.ClientSession()
        payload: Dict[str, Any] = {"query": synonym, "limit": 3}
        if object_type:
            payload["object_type"] = object_type
        if exclude_db_id:
            payload["exclude_db_id"] = exclude_db_id
        try:
            async with sess.post(f"{self._backend_url}/objects/similar", json=payload, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning(f"similar objects HTTP {resp.status}")
                    return []
                data = await resp.json(content_type=None)
                return data.get("objects") or []
        except Exception as e:
            logger.warning(f"similar objects failed: {e}")
            return []
        finally:
            if own_session:
                await sess.close()

    def _is_zero_result(self, slots: Dict[str, Any], result: dict, search_data: dict) -> bool:
        modality = slots.get("modality", "Текст")
        if modality == "Изображение":
            return not result.get("images")
        if modality == "Геоданные":
            return not result.get("map") and not result.get("objects")
        return not (search_data.get("objects") or [])

    async def _send_to_stand(self, search_data: dict) -> None:
        resources: List[dict] = search_data.get("resources") or []
        external_ids = [
            r["external_id"]
            for r in resources
            if isinstance(r, dict) and r.get("external_id")
        ]
        if not external_ids:
            return
        payload = {
            "items": [{"id": eid} for eid in external_ids],
            "secret_key": STAND_SECRET_KEY,
        }
        try:
            own_session = self._session is None
            sess = self._session or aiohttp.ClientSession()
            try:
                async with sess.post(API_URLS["stand_endpoint"], json=payload, timeout=5) as resp:
                    if not resp.ok:
                        logger.warning(f"Stand error: {resp.status}")
            finally:
                if own_session:
                    await sess.close()
        except Exception as e:
            logger.error(f"Stand connection error: {e}")

    # ── Форматирование результата ─────────────────────────────────────────────

    def _format_result(
        self,
        slots: Dict[str, Any],
        search_data: dict,
        promo_objects: list = None,
        *,
        object_offset: int = 0,
        resource_offset: int = 0,
    ) -> dict:
        llm = search_data.get("llm_answer") or {}
        llm_raw = (llm.get("content") or "").strip() if isinstance(llm, dict) else ""

        resources: List[dict] = search_data.get("resources") or []
        text_res = [r for r in resources if r.get("modality_type") == "Текст"]
        image_res = [r for r in resources if r.get("modality_type") == "Изображение"]
        geo_res = [r for r in resources if r.get("modality_type") == "Геоданные"]

        objects: List[dict] = search_data.get("objects") or []

        result: dict = {}
        text_title = ""

        if llm_raw:
            result["answer"] = self._clean_text(llm_raw)
        elif text_res:
            extracted = self._extract_text(text_res[0])
            if extracted:
                result["answer"] = extracted
                text_title = text_res[0].get("title") or ""
            elif not objects:
                result["answer"] = "По вашему запросу ничего не найдено."
        elif not objects:
            result["answer"] = "По вашему запросу ничего не найдено."

        images = []
        for r in image_res:
            content = r.get("content")
            if not isinstance(content, dict):
                continue
            if int((r.get("features") or {}).get("В стоп-листе") or 0):
                continue
            url = content.get("file_path") or content.get("url")
            if not url or url in images:
                continue
            images.append(url)
        if not images:
            logger.warning("All images are stop-listed — fallback to unfiltered")
            for r in image_res:
                content = r.get("content")
                if not isinstance(content, dict):
                    continue
                url = content.get("file_path") or content.get("url")
                if url and url not in images:
                    images.append(url)
        if images:
            result["images"] = images[:IMAGE_PAGE_SIZE]
        if text_title:
            result["text_title"] = text_title

        for r in geo_res:
            raw_links = (r.get("content") or {}).get("map_links")
            logger.debug(f"map_links raw: {raw_links!r}")
            if raw_links:
                result["map"] = _normalize_map_links(raw_links)
                logger.info(f"map normalized: {result['map']}")
                break

        if "map" not in result:
            for obj in objects:
                props = obj.get("properties") or {}
                if props.get("static_map") or props.get("interactive_map"):
                    result["map"] = {
                        "static": props.get("static_map"),
                        "interactive": props.get("interactive_map"),
                    }
                    break

        if "map" not in result:
            logger.info(f"map: не найдено (geo_res={len(geo_res)}, objects={len(objects)})")

        if objects:
            result["objects"] = [
                {
                    "id": obj.get("id"),
                    "db_id": obj.get("db_id"),
                    "name": obj["synonyms"][0].title() if obj.get("synonyms") else "—",
                }
                for obj in objects
            ]

        if "total_objects" in search_data:
            result["total_found"] = search_data["total_objects"]
        if "place_name" in search_data:
            result["place"] = search_data["place_name"]

        if promo_objects:
            result["promo"] = [
                {
                    "name": obj.get("name"),
                    "object_type": obj.get("object_type"),
                    "promo_text": obj.get("promo_text"),
                    "db_id": obj.get("db_id"),
                }
                for obj in promo_objects
            ]

        # Модальность «Изображение», но фотографий не нашлось: не подменяем ответ
        # текстом из RAG-фолбэка, а явно сообщаем, что фото нет.
        if slots.get("modality") == "Изображение" and not result.get("images"):
            result.pop("answer", None)
            result.pop("map", None)
            result["no_images"] = True
            names = [
                o.get("name")
                for o in (result.get("objects") or [])
                if o.get("name") and o.get("name") != "—"
            ]
            if names:
                shown = ", ".join(names[:3])
                if len(names) > 3:
                    shown += " и др."
                result["answer"] = IMAGE_NOT_FOUND_OBJECTS.replace("{names}", shown)
            else:
                synonym = (slots.get("synonym") or "").strip()
                result["answer"] = (
                    IMAGE_NOT_FOUND_SYNONYM.replace("{synonym}", synonym)
                    if synonym
                    else IMAGE_NOT_FOUND_GENERIC
                )

        result["more"] = self._build_more(
            search_data=search_data,
            result=result,
            text_res=text_res,
            objects=objects,
            llm_raw=llm_raw,
            object_offset=object_offset,
            resource_offset=resource_offset,
        )

        return result

    def _build_more(
        self,
        *,
        search_data: dict,
        result: dict,
        text_res: List[dict],
        objects: List[dict],
        llm_raw: str,
        object_offset: int,
        resource_offset: int,
    ) -> dict | None:
        # Для запроса фото без картинок пагинация не нужна: «Покажи ещё»
        # подразумевало бы ещё фотографии, которых нет.
        if result.get("no_images"):
            return None

        res_page = search_data.get("resources_pagination") or {}
        obj_page = search_data.get("pagination") or {}
        total_resources = int(search_data.get("total_resources") or res_page.get("total") or 0)

        if not search_data.get("object_criteria") and "place_name" not in search_data:
            return None

        object_limit = int(obj_page.get("limit") or OBJECT_PAGE_SIZE)
        object_has_more = bool(obj_page.get("has_more"))
        next_object_offset = int(
            obj_page.get("next_offset")
            or (max(0, object_offset) + object_limit)
        )

        images = result.get("images") or []
        if images:
            batch = len(images)
            total = int(res_page.get("total") or total_resources or batch)
            shown = max(0, resource_offset) + batch
            start = max(1, shown - batch + 1)
            return {
                "kind": "images",
                "shown": shown,
                "total": total,
                "batch": batch,
                "has_more": (shown < total) or object_has_more,
                "next_resource_offset": shown,
                "object_offset": max(0, object_offset),
                "object_limit": object_limit,
                "object_has_more": object_has_more,
                "next_object_offset": next_object_offset,
                "note": f"Показаны изображения {start}–{shown} из {total}.",
            }

        if text_res:
            if llm_raw and resource_offset <= 0:
                shown = 0
            else:
                shown = max(0, resource_offset) + 1
            total = int(res_page.get("total") or total_resources or len(text_res))
            note = ""
            if resource_offset > 0:
                note = f"Дополнительная информация, фрагмент {shown} из {total}."
            return {
                "kind": "text",
                "shown": shown,
                "total": total,
                "batch": 0,
                "has_more": (shown < total) or object_has_more,
                "next_resource_offset": shown,
                "object_offset": max(0, object_offset),
                "object_limit": object_limit,
                "object_has_more": object_has_more,
                "next_object_offset": next_object_offset,
                "note": note,
            }

        if objects:
            total = int(obj_page.get("total") or search_data.get("total_objects") or len(objects))
            shown = max(0, object_offset) + len(objects)
            has_more = object_has_more if obj_page else (shown < total)
            note = ""
            if object_offset > 0:
                note = f"Показаны объекты {max(1, object_offset + 1)}–{shown} из {total}."
            return {
                "kind": "place" if "place_name" in search_data else "objects",
                "shown": shown,
                "total": total,
                "batch": len(objects),
                "has_more": has_more,
                "next_object_offset": shown,
                "object_offset": max(0, object_offset),
                "object_limit": object_limit,
                "object_has_more": has_more,
                "next_resource_offset": 0,
                "note": note,
            }

        return None

    @staticmethod
    def _clean_text(text: str) -> str:
        """Убирает markdown-медиа из LLM-ответа, оставляет читаемый текст."""
        text = _MD_IMG_RE.sub("", text)                                  # удаляем ![alt](url)
        text = _MD_LINK_RE.sub(r"\1", text)                              # [text](url) → text
        text = re.sub(r"^\s*[-*]\s*$", "", text, flags=re.MULTILINE)    # пустые list-items
        text = _EXCESS_NL_RE.sub("\n\n", text)
        return text.strip()

    @staticmethod
    def _extract_text(resource: dict) -> str:
        c = resource.get("content") or {}
        if not isinstance(c, dict):
            return str(c).strip()

        sd = c.get("structured_data") or {}
        if not isinstance(sd, dict):
            return str(sd).strip()

        direct = sd.get("content") or sd.get("text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()

        parts: list[str] = []
        seen: set[str] = set()

        def walk(value) -> None:
            if isinstance(value, dict):
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
            elif isinstance(value, str):
                text = value.strip()
                if text and text not in seen:
                    seen.add(text)
                    parts.append(text)

        walk(sd)
        return "\n\n".join(parts).strip()

    # ── Роутинг ───────────────────────────────────────────────────────────────

    def _use_place_endpoint(self, slots: Dict[str, Any]) -> bool:
        """Геопоиск по месту: Геоданные + нет конкретного объекта + есть топоним."""
        if slots.get("modality") != "Геоданные":
            return False
        if slots.get("synonym"):
            return False
        props = slots.get("properties") or {}
        return bool(props.get("Детальное расположение") or props.get("Расположение относительно Байкала"))

    # ── Геопоиск по месту (/search/place/objects) ─────────────────────────────

    def _build_place_params(
        self,
        slots: Dict[str, Any],
        user_query: str,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict:
        props = slots.get("properties") or {}
        is_baikal = bool(props.get("Расположение относительно Байкала"))

        place_name = "озеро байкал" if is_baikal else (props.get("Детальное расположение") or "").lower()
        if not place_name:
            logger.warning("_build_place_params: place_name is empty — slot missing location")
        search_type = self._resolve_search_type(user_query, is_baikal)
        buffer_km = 10.0

        body: dict = {
            "place_name": place_name,
            "buffer_radius_km": buffer_km,
            "limit": limit or OBJECT_PAGE_SIZE,
            "offset": offset,
            "search_type": search_type,
        }

        # DB-свойства объекта без геолокационных ключей (они только для place_name)
        object_props = {k: v for k, v in props.items() if k not in _LOCATION_KEYS}
        subtypes = object_props.pop("Подтип объекта", None)

        if object_props:
            # Есть дополнительные фильтры (напр. Редкость) → object_criteria.properties
            criteria_props = dict(object_props)
            if subtypes:
                criteria_props["Подтип объекта"] = [subtypes] if isinstance(subtypes, str) else list(subtypes)
            body["object_criteria"] = {"properties": criteria_props}
        elif subtypes:
            # Только подтип — обратная совместимость с find_objects_with_geometry_by_subtypes
            body["Подтип объекта"] = [subtypes] if isinstance(subtypes, str) else list(subtypes)

        return body

    @staticmethod
    def _resolve_search_type(query: str, is_baikal: bool) -> str:
        if is_baikal:
            return "both"
        if _NEAR_RE.search(query):
            return "near"
        return "inside"

    async def _call_place(self, body: dict) -> dict:
        own_session = self._session is None
        sess = self._session or aiohttp.ClientSession()
        url = f"{self._backend_url}/search/place/objects"
        try:
            async with sess.post(url, json=body) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise _ApiError(resp.status, url, text[:500], body)
                return await resp.json(content_type=None)
        finally:
            if own_session:
                await sess.close()

    # ── Стандартный поиск (/search) ───────────────────────────────────────────

    @staticmethod
    def _strip_nulls(obj):
        if isinstance(obj, dict):
            return {k: SlotSearchExecutor._strip_nulls(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, list):
            return [SlotSearchExecutor._strip_nulls(i) for i in obj if i is not None]
        return obj

    def _build_search_params(
        self,
        slots: Dict[str, Any],
        user_query: str,
        *,
        object_offset: int = 0,
        object_limit: int | None = None,
        resource_offset: int = 0,
        resource_limit: int | None = None,
        use_llm_answer: bool | None = None,
        include_resources: bool = True,
    ) -> dict:
        search_params = {}

        if resource_limit is None and slots.get("modality") == "Изображение":
            resource_limit = IMAGE_PAGE_SIZE

        obj = {}
        synonym = slots.get("synonym")
        promo_db_id = slots.get("promo_db_id")
        promo_name = slots.get("promo_name", "")
        if promo_db_id:
            # Запрос ссылается на промо-объект — ищем по db_id напрямую
            obj["identificator"] = {"db_id": promo_db_id}
        elif synonym:
            names = [n.strip().lower().replace("ё", "е")
                     for n in re.split(r",|;|\s+и\s+", str(synonym)) if n.strip()]
            obj["name_synonyms"] = {"ru": names[:5]}
            # При синониме отбрасываем только класс-уровневые ключи — они фильтры
            # для поиска класса объектов и режут выборку через AND. Локацию и
            # атрибуты (Редкость, Детальное расположение) оставляем.
            _CLASS_LEVEL = {"Тип ОФФ", "Подтип объекта", "подтип объекта"}
            _merged = {**slots.get("properties", {}), **slots.get("extra", {})}
            _merged = {k: v for k, v in _merged.items() if k not in _CLASS_LEVEL}
            if _merged:
                obj["properties"] = _merged
            if slots.get("object_type"):
                obj["object_type"] = slots["object_type"]
        else:
            merged_props = {**slots.get("properties", {}), **slots.get("extra", {})}
            if merged_props:
                obj["properties"] = merged_props
            if slots.get("object_type"):
                obj["object_type"] = slots["object_type"]
        if obj:
            search_params["object"] = obj

        if slots.get("features"):
            search_params["resource"] = {"features": slots["features"]}

        if slots.get("modality"):
            search_params["modality_type"] = slots["modality"]

        # При промо-переходе подставляем имя объекта в user_query —
        # векторный поиск в salut_bot использует именно его, а не clean_user_query

        if promo_name:
            effective_query = f"Расскажи про {promo_name}"
        elif slots.get("from_list") and slots.get("synonym"):
            effective_query = f"Расскажи про {slots['synonym']}"
        else:
            effective_query = user_query

        if use_llm_answer is None:
            use_llm = slots.get("modality") != "Изображение"
        else:
            use_llm = use_llm_answer

        system_parameters: dict = {
            "debug": True,
            "user_query": effective_query,
            "clean_user_query": effective_query,
            "use_llm_answer": use_llm,
            "limit": object_limit or OBJECT_PAGE_SIZE,
            "offset": object_offset,
        }
        if resource_offset:
            system_parameters["resource_offset"] = resource_offset
        if not include_resources:
            system_parameters["resource_limit"] = 0
        elif resource_limit is not None:
            system_parameters["resource_limit"] = resource_limit

        return {
            "system_parameters": system_parameters,
            "search_parameters": search_params,
        }

    async def _fetch_promo(self, objects: list, user_query: str = "") -> list:
        object_ids = [o.get("id") for o in objects if isinstance(o, dict) and o.get("id")]
        if not object_ids:
            return []
        own_session = self._session is None
        sess = self._session or aiohttp.ClientSession()
        try:
            body = {
                "object_ids": object_ids,
                "relation_type": self._promo_relation_type,
                "user_query": user_query,
            }
            async with sess.post(f"{self._backend_url}/objects/related", json=body) as resp:
                if resp.ok:
                    data = await resp.json()
                    return data.get("related", [])
        except Exception as e:
            logger.warning(f"Promo fetch failed: {e}")
        finally:
            if own_session:
                await sess.close()
        return []

    async def _call_search(self, body: dict) -> dict:
        own_session = self._session is None
        sess = self._session or aiohttp.ClientSession()
        url = f"{self._backend_url}/search"
        try:
            async with sess.post(url, json=self._strip_nulls(body)) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise _ApiError(resp.status, url, text[:500], body)
                return await resp.json(content_type=None)
        finally:
            if own_session:
                await sess.close()