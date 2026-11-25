"""Справочник FAQ для smalltalk-ответов.

Хранится в JSON (путь задаётся FAQ_PATH), редактируется из админки через
backend /faq. Внешних зависимостей и векторного поиска нет: для десятков
вопросов достаточно нормализации, алиасов и нечёткого сравнения.

Ответ из FAQ детерминирован: при попадании текст отдаётся байт-в-байт,
без вызова LLM. Если совпадения нет — вызывающий код уходит в генерацию.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
_EXTRA_CHARS = str.maketrans({"ё": "е", "Ё": "Е"})

# Порог нечёткого совпадения: ниже — считаем, что это не про нас.
_MATCH_THRESHOLD = 0.6


def _normalize(text: str) -> str:
    text = (text or "").translate(_EXTRA_CHARS).lower()
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> List[str]:
    return _WORD_RE.findall(_normalize(text))


class FAQService:
    """Ленивый синглтон: файл перечитывается, если изменился на диске."""

    _instance: Optional["FAQService"] = None

    def __init__(self, path: Optional[str] = None):
        self._path = Path(path or os.getenv("FAQ_PATH", "faq/faq.json"))
        self._items: List[Dict[str, Any]] = []
        self._mtime: Optional[float] = None
        self._lock = threading.Lock()
        self._load()

    @classmethod
    def get(cls) -> "FAQService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Загрузка / сохранение ─────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if not self._path.exists():
                logger.warning("FAQ не найден: %s", self._path)
                self._items = []
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._items = self._validate(data)
            self._mtime = self._path.stat().st_mtime
            logger.info("FAQ загружен: %s записей (%s)", len(self._items), self._path)
        except Exception as exc:
            logger.error("Не удалось загрузить FAQ %s: %s", self._path, exc)
            self._items = []

    def _ensure_fresh(self) -> None:
        """Подхватывает правки файла, если их сделали мимо POST /faq."""
        try:
            if not self._path.exists():
                return
            mtime = self._path.stat().st_mtime
            if mtime == self._mtime:
                return
            with self._lock:
                if mtime != self._mtime:
                    self._load()
        except Exception:
            pass

    @staticmethod
    def _validate(data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, dict):
            data = data.get("items", [])
        if not isinstance(data, list):
            raise ValueError("FAQ должен быть списком записей")

        items: List[Dict[str, Any]] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            question = str(raw.get("question") or "").strip()
            answer = str(raw.get("answer") or "").strip()
            if not question or not answer:
                continue
            aliases = raw.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            items.append({
                "id": str(raw.get("id") or f"faq_{len(items) + 1}"),
                "kind": str(raw.get("kind") or "chitchat"),
                "question": question,
                "aliases": [str(a).strip() for a in aliases if str(a).strip()],
                "answer": answer,
            })
        return items

    def items(self) -> List[Dict[str, Any]]:
        self._ensure_fresh()
        return list(self._items)

    def save(self, data: Any) -> int:
        items = self._validate(data)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(items, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
        with self._lock:
            self._items = items
            self._mtime = self._path.stat().st_mtime
        logger.info("FAQ сохранён: %s записей", len(items))
        return len(items)

    # ── Поиск ─────────────────────────────────────────────────────────────────

    def find(self, query: str) -> Optional[Dict[str, Any]]:
        """Возвращает подходящую запись FAQ или None."""
        self._ensure_fresh()
        q_norm = _normalize(query)
        if not q_norm:
            return None

        q_tokens = set(_tokens(query))
        best: Optional[Dict[str, Any]] = None
        best_score = 0.0

        for item in self._items:
            for candidate in [item["question"], *item.get("aliases", [])]:
                c_norm = _normalize(candidate)
                if not c_norm:
                    continue

                # 1. Точное совпадение или вхождение осмысленного алиаса.
                if c_norm == q_norm or (len(c_norm) > 5 and c_norm in q_norm):
                    return item

                c_tokens = set(_tokens(candidate))
                if not c_tokens or not q_tokens:
                    continue

                # 2. Пересечение токенов (устойчиво к «а кто тебя вообще сделал»).
                overlap = len(c_tokens & q_tokens) / len(c_tokens | q_tokens)
                # 3. Символьная похожесть — страховка от опечаток.
                ratio = difflib.SequenceMatcher(None, q_norm, c_norm).ratio()
                score = max(overlap, ratio * 0.9)
                if score > best_score:
                    best_score = score
                    best = item

        return best if best_score >= _MATCH_THRESHOLD else None
