#!/usr/bin/env python3
"""Быстрая проверка механизма small-talk / FAQ.

Запуск:
    python3 scripts/test_smalltalk.py           # только офлайн-проверки (секунды)
    python3 scripts/test_smalltalk.py --live    # + живой прогон через core-api в Docker

Что проверяет офлайн:
    - строгое сопоставление FAQ (как перед роутером): точные вопросы и парафразы
      находят правило, а доменные вопросы («что это за растение?») — нет;
    - мягкое сопоставление (как в /smalltalk): находит больше вариантов.

Что проверяет --live (нужен запущенный стек `docker compose up`):
    - точный вопрос из FAQ отвечает из справочника (matched_by=faq);
    - приветствие уходит в smalltalk;
    - поисковый запрос не перехватывается FAQ;
    - продолжение про объект («а у неё как дела») остаётся поиском.

Код возврата 0 — всё прошло, 1 — есть провалы.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAQ_PATH = ROOT / "salut_bot" / "faq" / "faq.json"
FAQ_SERVICE_PATH = ROOT / "salut_bot" / "search_api" / "services" / "faq_service.py"

# (запрос, ожидаемый id правила или None)
STRICT_CASES = [
    ("кто тебя создал?", "creators"),
    ("а кто тебя вообще сделал", "creators"),
    ("как тебя зовут?", "name"),
    ("что ты умеешь?", "abilities"),
    ("откуда ты берёшь информацию?", "data_source"),
    ("что это за бот?", "what_is"),
    # не должны перехватываться FAQ
    ("что это за растение?", None),
    ("покажи фото нерпы", None),
    ("музеи в Иркутске", None),
    ("как дела", None),
]

LOOSE_CASES = [
    ("кто тебя создал?", "creators"),
    ("как тебя зовут", "name"),
    ("как дела", None),
    ("покажи фото нерпы", None),
]

# Живые кейсы: route — ожидаемый route в slots; faq_id — ожидаемое правило;
# search — запрос должен уйти в поиск (без route); first — предварительный запрос.
LIVE_CASES = [
    {"q": "кто тебя создал?", "route": "SMALLTALK", "faq_id": "creators"},
    {"q": "как дела", "route": "SMALLTALK"},
    {"q": "покажи фото нерпы", "search": True},
    {"q": "а у неё как дела", "first": "расскажи про нерпу", "search": True},
]


def load_faq_service():
    spec = importlib.util.spec_from_file_location("faq_service", FAQ_SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FAQService(str(FAQ_PATH))


def check_offline(service) -> int:
    failures = 0
    print("\n=== Офлайн: строгое сопоставление (как перед роутером) ===")
    for query, expected in STRICT_CASES:
        hit = service.find(query, strict=True)
        got = hit.get("id") if hit else None
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'}  {query!r:38} -> {got!r} (ожидалось {expected!r})")

    print("\n=== Офлайн: мягкое сопоставление (как в /smalltalk) ===")
    for query, expected in LOOSE_CASES:
        hit = service.find(query, strict=False)
        got = hit.get("id") if hit else None
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'}  {query!r:38} -> {got!r} (ожидалось {expected!r})")
    return failures


def docker_compose() -> list[str] | None:
    if shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "compose", "version"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        probe = subprocess.run(["docker-compose", "version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["docker-compose"]
    return None


def check_live(compose: list[str]) -> int:
    print("\n=== Живой прогон через core-api (может занять до минуты) ===")

    inner = f'''
import json, sys, urllib.request

BASE = "http://localhost:5001"
CASES = {LIVE_CASES!r}


def call(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/search_pipeline",
        data=data,
        headers={{"Content-Type": "application/json"}},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


for case in CASES:
    try:
        if case.get("first"):
            call({{"query": case["first"], "user_id": "qa-smoke", "router_enabled": True}})
        result = call({{"query": case["q"], "user_id": "qa-smoke", "router_enabled": True}})
        print("RESULT " + json.dumps({{"q": case["q"], "resp": result}}, ensure_ascii=False))
    except Exception as exc:
        print("RESULT " + json.dumps({{"q": case["q"], "error": str(exc)}}, ensure_ascii=False))
'''

    started = time.time()
    try:
        proc = subprocess.run(
            compose + ["exec", "-T", "core-api", "python", "-"],
            input=inner,
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("  FAIL  таймаут живого прогона")
        return 1

    if proc.returncode != 0:
        print("  FAIL  не удалось выполнить запросы в контейнере core-api")
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        for line in tail:
            print("        " + line)
        print("        Проверьте: docker compose ps, логи core-api/backend.")
        return 1

    results = {}
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            payload = json.loads(line[len("RESULT "):])
            results[payload["q"]] = payload

    failures = 0
    for case in LIVE_CASES:
        query = case["q"]
        payload = results.get(query)
        if not payload:
            failures += 1
            print(f"  FAIL  {query!r:38} -> нет ответа от core-api")
            continue
        if payload.get("error"):
            failures += 1
            print(f"  FAIL  {query!r:38} -> ошибка: {payload['error']}")
            continue

        response = payload["resp"]
        slots = response.get("slots") or {}
        result = response.get("result") or {}
        route = slots.get("route")
        matched_by = slots.get("matched_by")
        faq = slots.get("faq") or {}
        answer = (result.get("answer") or "").strip()

        problems = []
        if case.get("route") and route != case["route"]:
            problems.append(f"route={route!r}, ожидался {case['route']!r}")
        if case.get("faq_id") and faq.get("id") != case["faq_id"]:
            problems.append(f"faq.id={faq.get('id')!r}, ожидался {case['faq_id']!r}")
        if case.get("matched_by") and matched_by != case["matched_by"]:
            problems.append(f"matched_by={matched_by!r}")
        if case.get("search"):
            if route:
                problems.append(f"перехвачено FAQ/роутером (route={route!r})")
            if not (answer or result.get("images") or result.get("objects") or result.get("map")):
                problems.append("пустой результат поиска")
        elif not answer:
            problems.append("пустой текст ответа")

        if problems:
            failures += 1
            print(f"  FAIL  {query!r:38} -> {'; '.join(problems)}")
        else:
            if route:
                suffix = f" (route={route}, matched_by={matched_by})"
            else:
                bits = []
                if result.get("images"):
                    bits.append(f"фото={len(result['images'])}")
                if result.get("objects"):
                    bits.append(f"объектов={len(result['objects'])}")
                if result.get("map"):
                    bits.append("карта")
                if answer:
                    bits.append("текст")
                suffix = " (поиск" + (": " + ", ".join(bits) if bits else "") + ")"
            print(f"  OK    {query!r:38}{suffix}")

    print(f"\n  Живой прогон занял {time.time() - started:.1f} с")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Смоук-тест small-talk/FAQ")
    parser.add_argument("--live", action="store_true", help="дополнительно прогнать стек через core-api")
    args = parser.parse_args()

    if not FAQ_PATH.exists():
        print(f"Не найден файл FAQ: {FAQ_PATH}")
        return 1

    service = load_faq_service()
    failures = check_offline(service)

    if args.live:
        compose = docker_compose()
        if compose is None:
            print("\n=== Живой прогон: пропущен — docker compose не найден ===")
        else:
            failures += check_live(compose)

    total = len(STRICT_CASES) + len(LOOSE_CASES) + (len(LIVE_CASES) if args.live else 0)
    print("\n" + "=" * 60)
    if failures:
        print(f"ПРОВАЛЕНО: {failures} из {total}")
        return 1
    print(f"ВСЁ ПРОШЛО: {total} проверок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
