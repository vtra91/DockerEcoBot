#!/usr/bin/env python3
"""Тестировщик классификации изображений (properties/features).

Гоняет запросы через живой /classify несколько раз и показывает слоты,
чтобы увидеть нестабильность и перепутывание features <-> properties.

Запуск внутри core-api (там есть httpx и доступ к Redis):
    docker compose exec -T core-api python - < scripts/test_classification.py

Настройки через env:
    CLASSIFY_URL  (по умолчанию http://127.0.0.1:5001/classify)
    REPEATS       (по умолчанию 5)
    RAW=1         (печатать _debug_llm_raw)
"""

import json
import os
import re
import sys

import httpx

URL = os.getenv("CLASSIFY_URL", "http://127.0.0.1:5001/classify")
REPEATS = int(os.getenv("REPEATS", "5"))
SHOW_RAW = os.getenv("RAW", "").strip().lower() in {"1", "true", "yes"}

SEASON_KEYS = {"время года", "сезон", "сезон описанного события"}
SEASON_VALUES = {
    "зим": "Зима",
    "лет": "Лето",
    "весн": "Весна",
    "осен": "Осень",
}

# Ключи, которые обязаны жить в features, а не в properties
FEATURE_ONLY = {
    "время года", "сезон", "сезон описанного события",
    "сезонная активность", "внешние признаки", "цветение",
    "наличие плодов", "облачность", "темы текста",
}

# Запросы и ожидания. Поле "expect":
#   synonym      — подстрока в synonym
#   season       — ожидаемый сезон (по слову в запросе)
#   empty_features — features должен быть пустым
#   feature_key/value — ожидаемый признак
#   forbid_features — ключи, которых быть не должно
CASES = [
    ("покажи нерпу зимой", {"synonym": "нерпа", "season": "зим"}),
    ("покажи нерпу летом", {"synonym": "нерпа", "season": "лет"}),
    ("покажи крапивницу зимой", {"synonym": "крапивниц", "season": "зим",
                                  "forbid_features": ["сезонная активность"]}),
    ("покажи крапивницу", {"synonym": "крапивниц", "empty_features": True}),
    ("покажи нерпу", {"synonym": "нерпа", "empty_features": True}),
    ("покажи ежа", {"synonym": "еж", "empty_features": True}),
    ("покажи ежа в лесу", {"synonym": "еж", "feature_key": "среда обитания",
                            "feature_value": "лес"}),
    ("покажи фото бурого медведя в лесу", {"synonym": "медвед",
                                            "feature_key": "среда обитания",
                                            "feature_value": "лес"}),
    ("покажи нерпу на озере", {"synonym": "нерпа"}),
    ("покажи нерпу на льду зимой", {"synonym": "нерпа", "season": "зим"}),
]


def norm(value) -> str:
    return str(value).strip().lower().replace("ё", "е")


def feature_lookup(features: dict, key: str):
    """Ищет ключ признака без учёта регистра; возвращает (key, value) или (None, None)."""
    target = norm(key)
    for k, v in (features or {}).items():
        if norm(k) == target:
            return k, v
    return None, None


def evaluate(slots: dict, expect: dict) -> list:
    issues = []
    props = slots.get("properties") or {}
    feats = slots.get("features") or {}
    synonym = norm(slots.get("synonym") or "")

    if slots.get("error"):
        issues.append(f"error: {slots['error']}")
        return issues

    if "synonym" in expect and expect["synonym"] not in synonym:
        issues.append(f"synonym={slots.get('synonym')!r}, ожидали '{expect['synonym']}'")

    if "season" in expect:
        wanted = SEASON_VALUES[expect["season"]]
        key, value = None, None
        for k, v in feats.items():
            if norm(k) in SEASON_KEYS:
                key, value = k, v
                break
        if key is None:
            issues.append(f"нет сезона в features (features={feats})")
        elif norm(value) != norm(wanted):
            issues.append(f"сезон={value!r}, ожидали {wanted!r}")

    if expect.get("empty_features") and feats:
        issues.append(f"features должны быть пустыми, а там {feats}")

    if "feature_key" in expect:
        key, value = feature_lookup(feats, expect["feature_key"])
        if key is None:
            issues.append(f"нет признака '{expect['feature_key']}' (features={feats})")
        elif expect.get("feature_value") and norm(expect["feature_value"]) not in norm(value):
            issues.append(f"значение '{key}'={value!r}, ожидали ~{expect['feature_value']!r}")

    for forbidden in expect.get("forbid_features", []):
        key, _ = feature_lookup(feats, forbidden)
        if key is not None:
            issues.append(f"лишний признак '{key}'")

    # Перепутывание: признаки лежат в properties
    misplaced = [k for k in props if norm(k) in FEATURE_ONLY]
    if misplaced:
        issues.append(f"признаки в properties: {misplaced}")

    return issues


def main() -> int:
    client = httpx.Client(timeout=120.0)
    total_runs = 0
    total_ok = 0
    case_stats = []

    for query, expect in CASES:
        ok = 0
        issues_count = 0
        print(f"\n=== {query} ===")
        for i in range(1, REPEATS + 1):
            total_runs += 1
            try:
                resp = client.post(URL, json={"query": query})
                slots = resp.json()
            except Exception as exc:
                print(f"[{i}/{REPEATS}] HTTP error: {exc}")
                continue

            issues = evaluate(slots, expect)
            ok += 1 if not issues else 0
            issues_count += 1 if issues else 0
            props = slots.get("properties") or {}
            feats = slots.get("features") or {}
            print(
                f"[{i}/{REPEATS}] mod={slots.get('modality')} "
                f"syn={slots.get('synonym')!r} tpl={slots.get('template')} "
                f"props={json.dumps(props, ensure_ascii=False)} "
                f"feats={json.dumps(feats, ensure_ascii=False)}"
            )
            if issues:
                for issue in issues:
                    print(f"    ! {issue}")
            if SHOW_RAW and slots.get("_debug_llm_raw"):
                print(f"    raw: {json.dumps(slots['_debug_llm_raw'], ensure_ascii=False)}")
            total_ok += 1 if not issues else 0

        case_stats.append((query, ok, REPEATS))
        print(f"    итог: {ok}/{REPEATS} без замечаний")

    print("\n" + "=" * 60)
    for query, ok, n in case_stats:
        print(f"{ok}/{n}  {query}")
    print(f"ВСЕГО: {total_ok}/{total_runs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
