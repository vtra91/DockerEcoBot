#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fish_pipeline.py — загрузка рыб из merged.json и дозаполнение свойств и признаков.

Этап load:
  - object                 (рыба, object_properties)
  - object_name_synonym    (латинское и русское названия, родовые формы)
  - author, source, bibliographic, creation, resource_static
  - resource               (по одному на раздел: Описание, Распространение, ...)
  - text_value             (сам текст раздела)
  - support_metadata       (section, page, при необходимости number)
  - resource_value         (resource -> text_value, modality_id = Текст)
  - resource_object        (resource -> object, relation_type = 'описание')

Этап fill:
  - object.object_properties  (класс, отряд, семейство, род, вид, год и автор,
                               критерий отбора)
  - resource.features         (маркер раздела: {"образ жизни": 1.0})
  - resource_feature          (справочник имён признаков, --init-features)

Примеры:
  python fish_pipeline.py --init-features --dry-run
  python fish_pipeline.py
  python fish_pipeline.py --load
  python fish_pipeline.py --fill --print
  python fish_pipeline.py --dbname eco_clean
"""
import os, re, json, argparse, getpass
import psycopg2
from psycopg2.extras import Json

SCHEMA = "eco_assistant"

DBNAME = "eco"
DBHOST = "db"
DBPORT = 5432
DBUSER = "postgres"

TOOL = "load_fish.py"

TITLE_STYLE = "object_first"

LATIN_TAXONOMY_KEYS = True

# Признаки, которые заводятся в resource_feature (справочник для CMS).
# Значение 1.0 — заглушка
FEATURE_NAMES = [
    "описание и систематика",
    "распространение",
    "возраст и размеры",
    "образ жизни",
    "статус вида",
]
FEATURE_STUB = "1.0"          # в resource_feature.feature_values (text[])
FEATURE_MARK = 1.0            # в resource.features (jsonb)

# Заголовок раздела в support_metadata.parameters-'section'
SECTION_FEATURE = {
    "Описание":          "описание и систематика",
    "Распространение":   "распространение",
    "Возраст и размеры": "возраст и размеры",
    "Образ жизни":       "образ жизни",
    "Статус вида":       "статус вида",
}

# ============================================================ этап load

def _taxonomy_pairs():
    """Пары «ключ JSON -> ключ object_properties» для load."""
    fam_suffix = "латинское название" if LATIN_TAXONOMY_KEYS else "русское название"
    gen_suffix = fam_suffix
    return [
        ("русское название",              "вид (русское название)"),
        ("название на русском",           "вид (русское название)"),
        ("латинское название",            "вид (латинское название)"),
        ("семейство",                     f"семейство ({fam_suffix})"),
        ("семейство латинское",           "семейство (латинское название)"),
        ("род",                           f"род ({gen_suffix})"),
        ("род латинское",                 "род (латинское название)"),
        ("класс",                         "класс"),
        ("отряд",                         "отряд"),
        ("год и автор открытия",          "год и автор открытия"),
        ("критерий отбора",               "критерий отбора"),
    ]


FEATURES = _taxonomy_pairs()

SECTIONS = [
    ("описание и систематика", "Описание"),
    ("распространение",        "Распространение"),
    ("возраст и размеры",      "Возраст и размеры"),
    ("образ жизни",            "Образ жизни"),
    ("статус вида",            "Статус вида"),
]

ALIASES = {
    "описание и семантика":         "описание и систематика",
    "описание":                     "описание и систематика",
    "название на русском":          "русское название",
    "название русское":             "русское название",
    "семейство (русское название)": "семейство",
    "род (русское название)":       "род",
    "вид (русское название)":       "русское название",
    "вид (латинское название)":     "латинское название",
    "автор":                        "автор-составитель",
    "страница":                     "номер страницы, где описание",
}

# Варианты заголовка в тексте раздела 
HEADER_VARIANTS = {
    "описание и систематика": ["Описание и систематика", "Описание и семантика",
                               "Описание"],
    "распространение":        ["Распространение"],
    "возраст и размеры":      ["Возраст и размеры"],
    "образ жизни":            ["Образ жизни"],
    "статус вида":            ["Статус вида"],
}


def text(v):
    return "" if v is None else str(v).strip()


def normalize(row):
    out = {}
    for k, v in row.items():
        key = " ".join(k.strip().lower().replace("_", " ").split())
        out[ALIASES.get(key, key)] = v
    return out


def strip_header(body, json_key):
    """Срезает ведущий заголовок раздела: 'Описание и систематика. Более мелкий…'."""
    for variant in HEADER_VARIANTS.get(json_key, []):
        m = re.match(rf"^\s*{re.escape(variant)}\s*[.:]\s*", body, flags=re.IGNORECASE)
        if m:
            return body[m.end():].lstrip()
    return body


def connect(pwd, dbname):
    conn = psycopg2.connect(
        host=DBHOST, port=DBPORT, dbname=dbname, user=DBUSER,
        password=pwd, options=f"-c search_path={SCHEMA}",
    )
    conn.set_client_encoding("UTF8")
    return conn


def cols_map(cur):
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema=%s""", (SCHEMA,))
    d = {}
    for t, c in cur.fetchall():
        d.setdefault(t, set()).add(c)
    return d


def ins(cur, cols, table, **vals):
    """INSERT; updated_at добавляется, только если колонка есть."""
    names = ", ".join(vals)
    ph = ", ".join(["%s"] * len(vals))
    if "updated_at" in cols.get(table, set()):
        sql = (f"INSERT INTO {SCHEMA}.{table} ({names}, updated_at) "
               f"VALUES ({ph}, now()) RETURNING id")
    else:
        sql = f"INSERT INTO {SCHEMA}.{table} ({names}) VALUES ({ph}) RETURNING id"
    cur.execute(sql, list(vals.values()))
    return cur.fetchone()[0]


def get_or_create(cur, cols, table, key, value, **extra):
    cur.execute(f"SELECT id FROM {SCHEMA}.{table} WHERE {key}=%s ORDER BY id LIMIT 1", (value,))
    r = cur.fetchone()
    if r:
        return r[0], False
    return ins(cur, cols, table, **{key: value}, **extra), True


def synonym(cur, cols, object_id, syn, lang, prio):
    syn = text(syn)
    if not syn:
        return
    cur.execute(f"SELECT id FROM {SCHEMA}.object_name_synonym WHERE synonym=%s", (syn,))
    r = cur.fetchone()
    sid = r[0] if r else ins(cur, cols, "object_name_synonym", synonym=syn, language=lang)
    cur.execute(f"""INSERT INTO {SCHEMA}.object_name_synonym_link
                        (object_id, synonym_id, priority)
                    VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                (object_id, sid, prio))


def object_key(name_ru, name_lat):
    return "fish_" + (text(name_lat) or text(name_ru))


def exists_object(cur, name_ru, name_lat):
    """Дубль ищется по объекту, а не по справочнику синонимов."""
    cur.execute(f"""SELECT 1 FROM {SCHEMA}.object
                    WHERE lower(replace(db_id, ' ', '_')) = lower(%s) LIMIT 1""",
                (object_key(name_ru, name_lat),))
    if cur.fetchone():
        return True
    for s in (text(name_lat), text(name_ru)):
        if not s:
            continue
        cur.execute(f"""SELECT 1 FROM {SCHEMA}.object_name_synonym s
                        JOIN {SCHEMA}.object_name_synonym_link l ON l.synonym_id = s.id
                        WHERE s.synonym = %s LIMIT 1""", (s,))
        if cur.fetchone():
            return True
    return False


def find_source_by_page(cur, cols, page_num):
    """BibTeX-запись по точному номеру страницы. '{11}' не совпадёт с '{110}'."""
    page_num = text(page_num)
    if not page_num or page_num == "0":
        return get_or_create(cur, cols, "source", "name", "не указан")
    cur.execute(f"""SELECT id FROM {SCHEMA}.source
                    WHERE name ~ %s ORDER BY id LIMIT 1""",
                (rf"pages\s*=\s*\{{{re.escape(page_num)}\}}",))
    r = cur.fetchone()
    if r:
        return r[0], False
    return get_or_create(cur, cols, "source", "name", "не указан")


def resource_title(section, name_ru, name_lat):
    name = text(name_ru) or text(name_lat)
    if TITLE_STYLE == "object_first":
        return f"{name}: {section}"
    return f"{section}: {name}"


def load_one(cur, cols, row, ctx, args):
    name_ru = text(row.get("русское название"))
    name_lat = text(row.get("латинское название"))
    if not (name_ru or name_lat):
        raise ValueError("нет названия")
    if exists_object(cur, name_ru, name_lat):
        return "skipped"

    # автор -> author
    a_id, a_new = get_or_create(
        cur, cols, "author", "name",
        text(row.get("автор-составитель")) or "не указан")

    # источник -> source (по номеру страницы), затем bibliographic
    page_num = text(row.get("номер страницы, где описание"))
    s_id, s_new = find_source_by_page(cur, cols, page_num)

    cur.execute(f"""SELECT id FROM {SCHEMA}.bibliographic
                    WHERE author_id=%s AND source_id=%s ORDER BY id LIMIT 1""",
                (a_id, s_id))
    r = cur.fetchone()
    if r:
        b_id, b_new = r[0], False
    else:
        b_id = ins(cur, cols, "bibliographic", author_id=a_id, source_id=s_id)
        b_new = True

    # bibliographic + creation -> resource_static
    cur.execute(f"""SELECT id FROM {SCHEMA}.resource_static
                    WHERE bibliographic_id=%s AND creation_id=%s ORDER BY id LIMIT 1""",
                (b_id, ctx["creation_id"]))
    r = cur.fetchone()
    if r:
        st_id, st_new = r[0], False
    else:
        st_id = ins(cur, cols, "resource_static",
                    bibliographic_id=b_id, creation_id=ctx["creation_id"])
        st_new = True

    # свойства объекта
    props = {}
    for json_key, prop in FEATURES:
        v = text(row.get(json_key))
        if v:
            props[prop] = v
    props.setdefault("тип офф", "Объект фауны")

    object_id = ins(cur, cols, "object",
                    db_id=object_key(name_ru, name_lat),
                    object_type_id=ctx["type_id"],
                    object_properties=Json(props))

    # названия
    synonym(cur, cols, object_id, name_ru, "ru", 0)
    synonym(cur, cols, object_id, name_lat, "la", 1)
    if name_ru and " " in name_ru:
        synonym(cur, cols, object_id, name_ru.split()[0], "ru", 2)
    if name_lat and " " in name_lat:
        synonym(cur, cols, object_id, name_lat.split()[0], "la", 3)

    # пять разделов -> пять ресурсов
    number = text(row.get("номер"))
    for json_key, section in SECTIONS:
        body = text(row.get(json_key))
        if not body:
            continue
        if args.strip_headers:
            body = strip_header(body, json_key)

        tv = ins(cur, cols, "text_value", structured_data=Json({"text": body}))

        params = {"section": section}
        if page_num:
            params["page"] = page_num
        if args.store_number and number:
            params["number"] = number
        sm = ins(cur, cols, "support_metadata", parameters=Json(params))

        res = ins(cur, cols, "resource",
                  title=resource_title(section, name_ru, name_lat),
                  resource_static_id=st_id, support_metadata_id=sm)

        cur.execute(f"""INSERT INTO {SCHEMA}.resource_value
                            (resource_id, modality_id, value_id)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (resource_id, modality_id) DO NOTHING""",
                    (res, ctx["mod_id"], tv))

        cur.execute(f"""INSERT INTO {SCHEMA}.resource_object
                            (resource_id, object_id, relation_type)
                        VALUES (%s, %s, 'описание')
                        ON CONFLICT DO NOTHING""", (res, object_id))

    return "added", {"new_author": a_new, "new_source": s_new,
                     "new_biblio": b_new, "new_static": st_new}


def run_load(args, pwd):
    with open(args.file, encoding="utf-8-sig") as f:
        rows = [normalize(r) for r in json.load(f)]
    if args.limit:
        rows = rows[: args.limit]

    conn = connect(pwd, args.dbname)
    conn.autocommit = False
    cur = conn.cursor()
    cols = cols_map(cur)

    cur.execute(f"SELECT id FROM {SCHEMA}.object_type "
                f"WHERE name LIKE 'Объект флоры%' LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise SystemExit("в object_type нет записи 'Объект флоры…'")
    type_id = row[0]

    cur.execute(f"SELECT id FROM {SCHEMA}.modality "
                f"WHERE modality_type='Текст' LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise SystemExit("в modality нет записи 'Текст'")
    mod_id = row[0]

    creation_id, _ = get_or_create(cur, cols, "creation", "creation_tool", TOOL,
                                   creation_type="import",
                                   creation_params=Json({"file": args.file}))

    ctx = {"type_id": type_id, "mod_id": mod_id, "creation_id": creation_id}

    stats = {"added": 0, "skipped": 0, "errors": 0}
    for row in rows:
        cur.execute("SAVEPOINT row")
        try:
            res = load_one(cur, cols, row, ctx, args)
            if res == "skipped":
                stats["skipped"] += 1
            else:
                stats["added"] += 1
            cur.execute("RELEASE SAVEPOINT row")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT row")
            stats["errors"] += 1
            print("ОШИБКА",
                  row.get("русское название") or row.get("латинское название"),
                  "→", e)

    if args.dry_run:
        conn.rollback()
        print("[load] dry-run: изменения отменены")
    else:
        conn.commit()
    print("[load]", json.dumps({**stats, "dry_run": args.dry_run},
                               ensure_ascii=False, indent=2))
    cur.close()
    conn.close()


# ============================================================ этап fill

# Свойства: короткие значения, пишутся в object.object_properties
FILL_PROPS = [
    "класс", "отряд", "семейство", "семейство латинское",
    "род", "род латинское",
    "латинское название", "название на русском",
    "год и автор открытия", "критерий отбора",
]

FILL_PROP_MAP = {
    "латинское название":   "вид (латинское название)",
    "название на русском":  "вид (русское название)",
    "семейство":            ("семейство (латинское название)" if LATIN_TAXONOMY_KEYS
                             else "семейство (русское название)"),
    "семейство латинское":  "семейство (латинское название)",
    "род":                  ("род (латинское название)" if LATIN_TAXONOMY_KEYS
                             else "род (русское название)"),
    "род латинское":        "род (латинское название)",
}

# В признаки больше не пишется ничего содержательного
FILL_SKIP = {"номер", "номер страницы, где описание"}


def norm_key(k):
    return " ".join(k.replace("_", " ").split())


def split(row):
    """Возвращает только свойства. Тексты разделов идут в text_value."""
    props = {}
    for k, v in row.items():
        key = norm_key(k)
        key = ALIASES.get(key, key)
        if key in FILL_SKIP:
            continue
        if v is None or str(v).strip() == "":
            continue
        if key not in FILL_PROPS:
            continue
        props.setdefault(FILL_PROP_MAP.get(key, key), str(v).strip())
    return props


def table_exists(cur, name):
    cur.execute("""SELECT 1 FROM information_schema.tables
                   WHERE table_schema=%s AND table_name=%s""", (SCHEMA, name))
    return cur.fetchone() is not None


def ensure_features(cur, modality_id):
    """Заводит пять признаков в resource_feature, если их там ещё нет."""
    cur.execute(f"""INSERT INTO {SCHEMA}.resource_feature
                        (modality_id, feature_name, feature_values)
                    SELECT %s, n, ARRAY[%s]::text[]
                    FROM unnest(%s::text[]) AS n
                    ON CONFLICT (modality_id, feature_name) DO NOTHING""",
                (modality_id, FEATURE_STUB, FEATURE_NAMES))
    return cur.rowcount


def run_fill(args, pwd):
    with open(args.file, encoding="utf-8-sig") as f:
        rows = json.load(f)

    conn = connect(pwd, args.dbname)
    conn.autocommit = False
    cur = conn.cursor()

    if not table_exists(cur, "resource_object"):
        raise SystemExit("нет таблицы resource_object")

    cur.execute(f"SELECT id FROM {SCHEMA}.modality WHERE modality_type='Текст' LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise SystemExit("в modality нет записи 'Текст'")
    mod_id = row[0]

    if args.init_features:
        added = ensure_features(cur, mod_id)
        print(f"[fill] в resource_feature добавлено строк: {added}")

    st = {"records": len(rows), "objects_found": 0, "objects_missing": [],
          "sections_missing": [], "props_written": 0, "markers_written": 0,
          "errors": []}

    for row in rows:
        props = split(row)
        latin = norm_key(text(row.get("латинское название"))).strip()
        label = latin or props.get("вид (русское название)", "?")

        cur.execute("SAVEPOINT r")
        try:
            key = "fish_" + latin.replace(" ", "_")
            cur.execute(f"""SELECT id FROM {SCHEMA}.object
                            WHERE lower(replace(db_id, ' ', '_')) = lower(%s)""",
                        (key,))
            r = cur.fetchone()
            if not r:
                st["objects_missing"].append(label)
                cur.execute("RELEASE SAVEPOINT r")
                continue
            obj_id = r[0]
            st["objects_found"] += 1

            # свойства
            if props:
                cur.execute(f"""UPDATE {SCHEMA}.object
                                SET object_properties =
                                    COALESCE(object_properties, '{{}}'::jsonb) || %s
                                WHERE id = %s""", (Json(props), obj_id))
                st["props_written"] += cur.rowcount

            # маркеры разделов в resource.features
            found = 0
            for section, feature in SECTION_FEATURE.items():
                cur.execute(f"""UPDATE {SCHEMA}.resource r
                                SET features = jsonb_build_object(%s::text, %s::numeric)
                                FROM {SCHEMA}.resource_object ro,
                                     {SCHEMA}.support_metadata sm
                                WHERE ro.resource_id = r.id
                                  AND sm.id = r.support_metadata_id
                                  AND ro.object_id = %s
                                  AND sm.parameters->>'section' = %s""",
                            (feature, FEATURE_MARK, obj_id, section))
                found += cur.rowcount
                st["markers_written"] += cur.rowcount

            if found < len(SECTION_FEATURE):
                st["sections_missing"].append(f"{label}: {found}/{len(SECTION_FEATURE)}")

            if args.show:
                print(f"[{obj_id}] {label}")
                print(f"    свойства: {props}")
                print(f"    разделов отмечено: {found}")

            cur.execute("RELEASE SAVEPOINT r")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT r")
            st["errors"].append(f"{label}: {e}")

    if args.dry_run:
        conn.rollback()
        print("[fill] dry-run: изменения отменены")
    else:
        conn.commit()
    print("[fill]", json.dumps(st, ensure_ascii=False, indent=2))
    cur.close()
    conn.close()


# ============================================================ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="merged.json")
    ap.add_argument("--load", action="store_true", help="только загрузка")
    ap.add_argument("--fill", action="store_true", help="только свойства и признаки")
    ap.add_argument("--limit", type=int, default=0, help="только для --load")
    ap.add_argument("--print", dest="show", action="store_true", help="только для --fill")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dbname", default=DBNAME)

    ap.add_argument("--init-features", dest="init_features", action="store_true",
                    help="завести пять признаков в resource_feature")
    ap.add_argument("--strip-headers", dest="strip_headers", action="store_true",
                    help="срезать ведущий заголовок раздела из текста")
    ap.add_argument("--store-number", dest="store_number", action="store_true",
                    help="писать поле 'номер' в support_metadata.parameters")
    args = ap.parse_args()

    both = not (args.load or args.fill)
    pwd = os.environ.get("PGPASSWORD") or getpass.getpass("Пароль PostgreSQL: ")

    if both or args.load:
        run_load(args, pwd)
    if both or args.fill:
        run_fill(args, pwd)


if __name__ == "__main__":
    main()