#!/usr/bin/env python3
"""Объёмный сценарный тест small-talk / FAQ.

Запуск:
    python3 scripts/test_smalltalk_full.py            # офлайн-матрица (секунды)
    python3 scripts/test_smalltalk_full.py --live     # + живые сценарии и проверки API

Офлайн-часть использует встроенную фикстуру FAQ (не зависит от ваших правок
справочника) и проверяет сам матчер: точные фразы, алиасы, парафразы,
пунктуацию, регистр, ё/е и «ловушки», которые НЕ должны перехватываться.

Живая часть (нужен запущенный стек):
    - многошаговые сценарии smalltalk ↔ поиск ↔ брань (с контекстом диалога);
    - FAQ: точное совпадение, алиас, пунктуация, детерминированность ответа;
    - проверка, что FAQ не перехватывает доменные запросы;
    - поведение при выключенном роутере (регресс старого режима);
    - поиск как таковой (музеи, услуги, фото) — не сломан ли;
    - прямые проверки backend: /faq, /faq/match, /smalltalk.

Живой прогон может занять несколько минут (реальные вызовы LLM).

Код возврата 0 — критичные проверки прошли, 1 — есть провалы.
Мягкие проверки (где LLM может ответить по-разному) печатаются как WARN
и не влияют на код возврата.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAQ_SERVICE_PATH = ROOT / "salut_bot" / "search_api" / "services" / "faq_service.py"
REAL_FAQ_PATH = ROOT / "salut_bot" / "faq" / "faq.json"

# ── Фикстура для офлайн-проверок ──────────────────────────────────────────────
FIXTURE = [
    {"id": "creators", "kind": "meta", "question": "Кто тебя создал?",
     "aliases": ["кто тебя сделал", "кто тебя придумал", "кто разработчик",
                 "кто автор", "чья разработка"],
     "answer": "Меня разработали для Байкальского музея СО РАН."},
    {"id": "name", "kind": "meta", "question": "Как тебя зовут?",
     "aliases": ["как зовут", "твоё имя", "твое имя", "кто ты"],
     "answer": "Я — Экоассистент Байкальского музея."},
    {"id": "abilities", "kind": "meta", "question": "Что ты умеешь?",
     "aliases": ["что ты можешь", "какие у тебя функции", "чем ты помогаешь"],
     "answer": "Я умею рассказывать о природе Байкала."},
    {"id": "what_is", "kind": "meta", "question": "Что это за бот?",
     "aliases": ["что за бот", "для чего ты", "что это"],
     "answer": "Это эко-ассистент по Байкальскому региону."},
    {"id": "data_source", "kind": "meta", "question": "Откуда ты берёшь информацию?",
     "aliases": ["откуда данные", "чьи данные", "на чём основан"],
     "answer": "Ответы строятся по базе знаний музея."},
    {"id": "contacts", "kind": "meta", "question": "Как связаться с музеем?",
     "aliases": ["телефон музея", "контакты музея"],
     "answer": "Телефон музея: 123-45-67."},
]

# (запрос, ожидаемый id или None, комментарий)
OFFLINE_STRICT = [
    # точные вопросы и регистр/пунктуация
    ("кто тебя создал?", "creators", "точный вопрос"),
    ("Кто тебя создал?", "creators", "заглавная буква"),
    ("КТО ТЕБЯ СОЗДАЛ?!!", "creators", "капс + пунктуация"),
    ("  кто тебя создал?  ", "creators", "пробелы по краям"),
    # алиасы
    ("кто разработчик", "creators", "алиас"),
    ("кто тебя придумал", "creators", "алиас"),
    ("чья разработка", "creators", "алиас"),
    ("как зовут", "name", "короткий алиас"),
    ("твое имя", "name", "ё→е в алиасе"),
    ("что ты можешь", "abilities", "алиас"),
    ("что за бот", "what_is", "алиас"),
    ("откуда данные", "data_source", "алиас"),
    ("на чем основан", "data_source", "ё→е"),
    ("контакты музея", "contacts", "алиас нового правила"),
    # парафразы
    ("а кто тебя вообще сделал", "creators", "парафраз с лишними словами"),
    ("ну и кто тебя создал то", "creators", "разговорная парафраза"),
    ("как тебя зовут в музее?", "name", "вопрос с уточнением"),
    ("кто тебя создал и зачем?", "creators", "вопрос с добавкой"),
    # ловушки: не должны перехватываться
    ("что это за растение?", None, "доменный вопрос"),
    ("что это за животное?", None, "доменный вопрос"),
    ("что это за место?", None, "доменный вопрос"),
    ("что за ботаника?", None, "ловушка на «что за бот»"),
    ("как тебя найти?", None, "ловушка на «как зовут»"),
    ("как тебя зовут в лесу?", "name", "всё же про имя"),
    ("покажи фото нерпы", None, "поиск фото"),
    ("музеи в Иркутске", None, "поиск объектов"),
    ("режим работы музея", None, "поиск услуги"),
    ("как дела", None, "болтовня без правила"),
    ("привет", None, "приветствие"),
    ("спасибо", None, "благодарность"),
    ("расскажи про нерпу", None, "описание объекта"),
    ("нерпа?", None, "короткий поисковый"),
    ("откуда ты берёшь фотографии?", None, "ловушка на «откуда информацию»"),
]

OFFLINE_LOOSE = [
    ("кто тебя создал?", "creators", "точный"),
    ("как тебя зовут", "name", "вопрос"),
    ("что это за растение?", "what_is", "в мягком режиме ловушка срабатывает — это ожидаемо"),
    ("привет", None, "нет правила"),
    ("покажи фото нерпы", None, "поиск"),
]

# ── Живые сценарии ────────────────────────────────────────────────────────────
# turn: q, router (по умолчанию True), route, faq_id, matched_by,
#       answer_contains, search, allow_routes
SCENARIOS = [
    {"name": "smalltalk → поиск", "critical": True, "turns": [
        {"q": "как дела", "route": "SMALLTALK"},
        {"q": "покажи фото нерпы", "search": True},
    ]},
    {"name": "поиск → благодарность", "critical": True, "turns": [
        {"q": "расскажи про нерпу", "search": True},
        {"q": "спасибо", "route": "SMALLTALK"},
    ]},
    {"name": "продолжение про объект", "critical": True, "turns": [
        {"q": "расскажи про нерпу", "search": True},
        {"q": "а где она обитает?", "search": True},
        {"q": "а у неё как дела", "search": True},
    ]},
    {"name": "FAQ: точное совпадение", "critical": True, "turns": [
        {"q": "кто тебя создал?", "route": "SMALLTALK", "faq_id": "creators",
         "matched_by": "faq", "answer_contains": "музея"},
    ]},
    {"name": "FAQ: парафраз", "critical": True, "turns": [
        {"q": "а кто тебя вообще сделал", "route": "SMALLTALK", "faq_id": "creators"},
    ]},
    {"name": "FAQ: пунктуация и регистр", "critical": True, "turns": [
        {"q": "КТО ТЕБЯ СОЗДАЛ?!!", "route": "SMALLTALK", "faq_id": "creators"},
    ]},
    {"name": "FAQ: детерминированность", "critical": True, "turns": [
        {"q": "кто тебя создал?", "route": "SMALLTALK", "faq_id": "creators", "remember": "answer"},
        {"q": "кто тебя создал?", "route": "SMALLTALK", "faq_id": "creators", "same_as": "answer"},
    ]},
    {"name": "FAQ не перехватывает домен", "critical": True, "turns": [
        {"q": "что это за растение?", "search": True},
        {"q": "что это за животное?", "search": True},
    ]},
    {"name": "FAQ → доменный запрос", "critical": True, "turns": [
        {"q": "кто тебя создал?", "route": "SMALLTALK", "faq_id": "creators"},
        {"q": "покажи фото нерпы", "search": True},
    ]},
    {"name": "смешанное: привет + поиск", "critical": True, "turns": [
        {"q": "привет, покажи нерпу", "search": True},
    ]},
    {"name": "смешанное: спасибо + поиск", "critical": True, "turns": [
        {"q": "спасибо, а теперь покажи нерпу", "search": True},
    ]},
    {"name": "брань", "critical": False, "turns": [
        {"q": "ты тупой бот", "route": "ABUSE", "allow_routes": ["ABUSE", "SMALLTALK"]},
        {"q": "покажи нерпу", "search": True},
    ]},
    {"name": "брань → FAQ", "critical": True, "turns": [
        {"q": "ты тупой бот", "allow_routes": ["ABUSE", "SMALLTALK"]},
        {"q": "кто тебя создал?", "route": "SMALLTALK", "faq_id": "creators"},
    ]},
    {"name": "болтовня → болтовня → поиск", "critical": True, "turns": [
        {"q": "привет", "route": "SMALLTALK"},
        {"q": "как дела", "route": "SMALLTALK"},
        {"q": "покажи фото нерпы", "search": True},
    ]},
    {"name": "роутер выключен (регресс)", "critical": True, "turns": [
        {"q": "кто тебя создал?", "router": False, "search": True},
        {"q": "привет", "router": False, "route": "SMALLTALK"},
    ]},
    {"name": "поиск: объекты", "critical": True, "turns": [
        {"q": "музеи в Иркутске", "search": True},
    ]},
    {"name": "поиск: услуга", "critical": True, "turns": [
        {"q": "режим работы Байкальского музея", "search": True},
    ]},
    {"name": "FAQ: способности и имя", "critical": True, "turns": [
        {"q": "что ты умеешь?", "route": "SMALLTALK", "faq_id": "abilities"},
        {"q": "как тебя зовут?", "route": "SMALLTALK", "faq_id": "name"},
    ]},
    {"name": "короткий запрос про объект", "critical": False, "turns": [
        {"q": "нерпа?", "search": True},
    ]},
    {"name": "длинное сообщение", "critical": False, "turns": [
        {"q": "расскажи пожалуйста подробно про байкальскую нерпу и её образ жизни, "
              "чем питается, где обитает, сколько живёт и почему она считается символом Байкала",
         "search": True},
    ]},
]


def load_faq_service(module_path: Path):
    spec = importlib.util.spec_from_file_location("faq_service_full", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_offline() -> tuple[int, int]:
    """Возвращает (провалы, предупреждения)."""
    module = load_faq_service(FAQ_SERVICE_PATH)
    failures = 0
    warnings = 0

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "faq.json"
        path.write_text(json.dumps(FIXTURE, ensure_ascii=False), encoding="utf-8")
        service = module.FAQService(str(path))

        print("\n=== Офлайн: строгий режим (как перед роутером) ===")
        for query, expected, note in OFFLINE_STRICT:
            hit = service.find(query, strict=True)
            got = hit.get("id") if hit else None
            ok = got == expected
            failures += 0 if ok else 1
            mark = "OK  " if ok else "FAIL"
            print(f"  {mark}  {query!r:42} -> {got!r:12} ({note})")

        print("\n=== Офлайн: мягкий режим (как в /smalltalk) ===")
        for query, expected, note in OFFLINE_LOOSE:
            hit = service.find(query, strict=False)
            got = hit.get("id") if hit else None
            ok = got == expected
            failures += 0 if ok else 1
            mark = "OK  " if ok else "FAIL"
            print(f"  {mark}  {query!r:42} -> {got!r:12} ({note})")

    # Реальный справочник: файл должен читаться и не содержать дублей id
    print("\n=== Офлайн: реальный справочник ===")
    if not REAL_FAQ_PATH.exists():
        print(f"  FAIL  не найден {REAL_FAQ_PATH}")
        failures += 1
    else:
        try:
            items = json.loads(REAL_FAQ_PATH.read_text(encoding="utf-8"))
            ids = [str(x.get("id")) for x in items]
            dupes = {i for i in ids if ids.count(i) > 1}
            bad = [x for x in items if not x.get("question") or not x.get("answer")]
            print(f"  OK    записей: {len(items)}, файл читается")
            if dupes:
                print(f"  FAIL  повторяющиеся id: {sorted(dupes)}")
                failures += 1
            if bad:
                print(f"  FAIL  записей без question/answer: {len(bad)}")
                failures += 1
        except Exception as exc:
            print(f"  FAIL  не удалось прочитать справочник: {exc}")
            failures += 1

    return failures, warnings


def docker_compose() -> list[str] | None:
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        probe = subprocess.run(["docker-compose", "version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["docker-compose"]
    return None


def run_in_container(compose: list[str], service: str, code: str) -> tuple[bool, str, str]:
    try:
        proc = subprocess.run(
            compose + ["exec", "-T", service, "python", "-"],
            input=code,
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=1800,
        )
        return proc.returncode == 0, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return False, "", "таймаут"


def evaluate_turn(turn: dict, resp: dict, memory: dict) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []

    slots = resp.get("slots") or {}
    result = resp.get("result") or {}
    route = slots.get("route")
    matched_by = slots.get("matched_by")
    faq = slots.get("faq") or {}
    answer = (result.get("answer") or "").strip()
    allowed = turn.get("allow_routes")

    if allowed:
        if route not in allowed:
            warnings.append(f"route={route!r}, допустимо {allowed}")
    elif turn.get("route") and route != turn["route"]:
        problems.append(f"route={route!r}, ожидался {turn['route']!r}")

    if turn.get("faq_id") and faq.get("id") != turn["faq_id"]:
        problems.append(f"faq.id={faq.get('id')!r}, ожидался {turn['faq_id']!r}")

    if turn.get("matched_by") and matched_by != turn["matched_by"]:
        problems.append(f"matched_by={matched_by!r}, ожидался {turn['matched_by']!r}")

    if turn.get("answer_contains"):
        needle = turn["answer_contains"].lower()
        if needle not in answer.lower():
            problems.append(f"в ответе нет {turn['answer_contains']!r}")

    if turn.get("search"):
        if route:
            problems.append(f"перехвачено (route={route!r})")
        if not (answer or result.get("images") or result.get("objects") or result.get("map")):
            problems.append("пустой результат поиска")

    if turn.get("remember"):
        memory[turn["remember"]] = answer

    if turn.get("same_as"):
        expected = memory.get(turn["same_as"])
        if expected is not None and answer != expected:
            problems.append("ответ отличается от предыдущего (FAQ должен быть детерминирован)")

    if not answer and not turn.get("search"):
        problems.append("пустой текст ответа")

    if "timing" not in resp:
        warnings.append("в ответе нет timing")

    return problems, warnings


def check_live(compose: list[str]) -> tuple[int, int]:
    failures = 0
    warnings = 0

    print("\n=== Живые сценарии через core-api (может занять несколько минут) ===")
    inner = f'''
import json, urllib.request

BASE = "http://localhost:5001"
SCENARIOS = {SCENARIOS!r}


def call(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/search_pipeline",
        data=data,
        headers={{"Content-Type": "application/json"}},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


for si, scenario in enumerate(SCENARIOS):
    user_id = "qa-full-" + str(si)
    for ti, turn in enumerate(scenario["turns"]):
        payload = {{
            "query": turn["q"],
            "user_id": user_id,
            "router_enabled": turn.get("router", True),
        }}
        try:
            resp = call(payload)
            print("TURN " + json.dumps({{"s": si, "t": ti, "resp": resp}}, ensure_ascii=False))
        except Exception as exc:
            print("TURN " + json.dumps({{"s": si, "t": ti, "error": str(exc)}}, ensure_ascii=False))
'''

    print("  (это может занять несколько минут — идут реальные вызовы LLM)")
    started = time.time()
    ok, stdout, stderr = run_in_container(compose, "core-api", inner)
    if not ok:
        print("  FAIL  не удалось прогнать сценарии в core-api")
        for line in (stderr or stdout).strip().splitlines()[-6:]:
            print("        " + line)
        return 1, 0

    turns: dict[tuple[int, int], dict] = {}
    for line in stdout.splitlines():
        if line.startswith("TURN "):
            payload = json.loads(line[len("TURN "):])
            turns[(payload["s"], payload["t"])] = payload

    for si, scenario in enumerate(SCENARIOS):
        critical = scenario.get("critical", True)
        print(f"\n  Сценарий «{scenario['name']}»" + ("" if critical else " (мягкий)"))
        memory: dict = {}
        scenario_failed = 0
        for ti, turn in enumerate(scenario["turns"]):
            payload = turns.get((si, ti))
            label = turn["q"][:44]
            if not payload:
                print(f"    FAIL  {label!r:46} -> нет ответа")
                scenario_failed += 1
                continue
            if payload.get("error"):
                print(f"    FAIL  {label!r:46} -> ошибка: {payload['error']}")
                scenario_failed += 1
                continue

            problems, warns = evaluate_turn(turn, payload["resp"], memory)
            if problems:
                if critical:
                    print(f"    FAIL  {label!r:46} -> {'; '.join(problems)}")
                    scenario_failed += 1
                else:
                    print(f"    WARN  {label!r:46} -> {'; '.join(problems)}")
                    warnings += 1
            elif warns:
                print(f"    WARN  {label!r:46} -> {'; '.join(warns)}")
                warnings += 1
            else:
                print(f"    OK    {label!r:46}")

        failures += scenario_failed

    print(f"\n  Живые сценарии заняли {time.time() - started:.1f} с")
    return failures, warnings


def check_backend(compose: list[str]) -> tuple[int, int]:
    print("\n=== Прямые проверки backend ===")
    inner = '''
import json, urllib.request

BASE = "http://localhost:8000"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


out = {"faq": get("/faq")}
out["match_yes"] = post("/faq/match", {"query": "кто тебя создал?"})
out["match_no"] = post("/faq/match", {"query": "что это за растение?"})
out["smalltalk_faq"] = post("/smalltalk", {"query": "кто тебя создал?"})
out["smalltalk_llm"] = post("/smalltalk", {"query": "как дела"})
print("BACKEND " + json.dumps(out, ensure_ascii=False))
'''
    ok, stdout, stderr = run_in_container(compose, "backend", inner)
    if not ok:
        print("  FAIL  не удалось обратиться к backend")
        for line in (stderr or stdout).strip().splitlines()[-6:]:
            print("        " + line)
        return 1, 0

    payload = None
    for line in stdout.splitlines():
        if line.startswith("BACKEND "):
            payload = json.loads(line[len("BACKEND "):])
    if payload is None:
        print("  FAIL  backend не вернул данные проверок")
        return 1, 0

    failures = 0
    warnings = 0

    faq = payload.get("faq") or {}
    count = faq.get("count", len(faq.get("items") or []))
    if count:
        print(f"  OK    /faq: записей {count}, путь {faq.get('path')}")
    else:
        print("  FAIL  /faq: справочник пуст")
        failures += 1

    match_yes = (payload.get("match_yes") or {}).get("match")
    if match_yes and match_yes.get("id") == "creators":
        print("  OK    /faq/match: точный вопрос найден")
    else:
        print(f"  FAIL  /faq/match: ожидался creators, получено {match_yes!r}")
        failures += 1

    match_no = (payload.get("match_no") or {}).get("match")
    if match_no is None:
        print("  OK    /faq/match: доменный вопрос не перехвачен")
    else:
        print(f"  FAIL  /faq/match: доменный вопрос перехвачен правилом {match_no.get('id')!r}")
        failures += 1

    st_faq = payload.get("smalltalk_faq") or {}
    if st_faq.get("source") == "faq" and (st_faq.get("faq") or {}).get("id") == "creators":
        print("  OK    /smalltalk: ответ из FAQ")
    else:
        print(f"  FAIL  /smalltalk: ожидался source=faq/creators, получено {st_faq!r}")
        failures += 1

    st_llm = payload.get("smalltalk_llm") or {}
    if (st_llm.get("answer") or "").strip():
        print(f"  OK    /smalltalk: свободный ответ (source={st_llm.get('source')!r})")
    else:
        print("  FAIL  /smalltalk: пустой свободный ответ")
        failures += 1

    return failures, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Объёмный сценарный тест small-talk/FAQ")
    parser.add_argument("--live", action="store_true", help="дополнительно прогнать стек")
    parser.add_argument("--only", choices=["offline", "live", "backend"], help="выполнить только одну часть")
    args = parser.parse_args()

    failures = 0
    warnings = 0
    total = 0

    if args.only in (None, "offline"):
        f, w = check_offline()
        failures += f
        warnings += w
        total += len(OFFLINE_STRICT) + len(OFFLINE_LOOSE) + 1

    if args.live and args.only in (None, "live", "backend"):
        compose = docker_compose()
        if compose is None:
            print("\n=== Живая часть: пропущена — docker compose не найден ===")
        else:
            if args.only in (None, "live"):
                f, w = check_live(compose)
                failures += f
                warnings += w
                total += sum(len(s["turns"]) for s in SCENARIOS)
            if args.only in (None, "backend"):
                f, w = check_backend(compose)
                failures += f
                warnings += w
                total += 5

    print("\n" + "=" * 62)
    if failures:
        print(f"ПРОВАЛЕНО: {failures} критичных проверок (всего проверок: {total})")
        return 1
    print(f"КРИТИЧНЫХ ПРОВАЛОВ НЕТ. Проверок: {total}, предупреждений: {warnings}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
