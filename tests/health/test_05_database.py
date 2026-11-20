"""Уровень 4: база данных — та самая, с которой работает backend: расширения, схема, данные, целостность."""
import os

import pytest

EXPECTED_TABLES = [
    "object_type", "object", "object_name_synonym", "object_name_synonym_link", "object_object_link",
    "modality", "text_value", "image_value", "geodata_value", "resource_value",
    "author", "source", "reliability_level", "bibliographic", "creation", "resource_static",
    "support_metadata", "resource", "resource_object", "resource_resource_link",
    "object_property", "resource_feature",
    "resource_resource_relation_type", "object_object_relation_type", "resource_object_relation_type",
]
OBJECT_TYPES = ["Объект флоры и фауны", "Географический объект", "Услуга"]
# Импортёр сохраняет «Картографическая информация» из resources.json как «Геоданные»
MODALITIES = ["Текст", "Изображение", "Геоданные"]
MIN_OBJECTS = int(os.getenv("HEALTH_MIN_OBJECTS", "1000"))       # в json_files/objects.json ~7.8 тыс.
MIN_RESOURCES = int(os.getenv("HEALTH_MIN_RESOURCES", "1000"))   # в json_files/resources.json ~10.7 тыс.
KNOWN_OBJECTS = [x.strip() for x in os.getenv(
    "HEALTH_KNOWN_OBJECTS", "Байкальская нерпа,Байкальский омуль,Листвянка").split(",") if x.strip()]


def test_connected_to_expected_database(sql, db_params):
    (dbname, user), = sql("select current_database(), current_user")
    assert (dbname, user) == (db_params["dbname"], db_params["user"])


@pytest.mark.parametrize("ext", ["postgis", "vector", "pg_trgm"])
def test_extension_installed(sql, ext):
    rows = sql("select extversion from pg_extension where extname = %s", (ext,))
    assert rows, "Расширение %s не установлено в БД (db_custom/init.sql выполняется только на пустом томе)" % ext


def test_postgis_works(sql):
    (area,), = sql("select ST_Area(ST_MakeEnvelope(0, 0, 2, 3, 4326))")
    assert area == 6


def test_schema_tables_exist(sql):
    rows = sql("select table_name from information_schema.tables where table_schema = 'eco_assistant'")
    missing = sorted(set(EXPECTED_TABLES) - {r[0] for r in rows})
    assert not missing, "В схеме eco_assistant нет таблиц: %s — импорт (scripts/init_db.sh) не запускался?" % missing


def test_error_log_table_exists(sql):
    """public.error_log: туда пишет backend (/log_error), читает админка (/admin/logs)."""
    assert sql("select to_regclass('public.error_log')")[0][0], "Нет таблицы public.error_log"


def test_object_types_seeded(sql):
    names = {r[0] for r in sql("select name from eco_assistant.object_type")}
    missing = [t for t in OBJECT_TYPES if t not in names]
    assert not missing, "В object_type нет %s (на них завязаны константы *_OBJECT_TYPE_ID в админке)" % missing


def test_object_type_ids_match_admin_constants(sql):
    """AdminPanel/main.py: BIO_OBJECT_TYPE_ID=1, GEO=2, SERVICE=3 — захардкожено."""
    ids = dict(sql("select name, id from eco_assistant.object_type"))
    expected = dict(zip(OBJECT_TYPES, [1, 2, 3]))
    wrong = {k: ids.get(k) for k, v in expected.items() if ids.get(k) != v}
    assert not wrong, "id типов объектов не совпадают с константами админки: %s" % wrong


def test_modalities_present(sql):
    rows = dict(sql("select modality_type, value_table_name from eco_assistant.modality"))
    missing = [m for m in MODALITIES if m not in rows]
    assert not missing, "Нет модальностей %s (есть: %s)" % (missing, sorted(rows))


def test_objects_loaded(sql):
    (n,), = sql("select count(*) from eco_assistant.object")
    assert n >= MIN_OBJECTS, "Объектов %d < %d — БД не наполнена или наполнена частично" % (n, MIN_OBJECTS)


@pytest.mark.parametrize("type_name", OBJECT_TYPES)
def test_objects_of_each_type(sql, type_name):
    (n,), = sql("""select count(*) from eco_assistant.object o
                   join eco_assistant.object_type t on t.id = o.object_type_id where t.name = %s""", (type_name,))
    assert n > 0, "Нет ни одного объекта типа %r" % type_name


def test_resources_loaded(sql):
    (n,), = sql("select count(*) from eco_assistant.resource")
    assert n >= MIN_RESOURCES, "Ресурсов %d < %d" % (n, MIN_RESOURCES)


@pytest.mark.parametrize("modality", MODALITIES)
def test_resources_of_each_modality(sql, modality):
    (n,), = sql("""select count(*) from eco_assistant.resource_value rv
                   join eco_assistant.modality m on m.id = rv.modality_id where m.modality_type = %s""", (modality,))
    assert n > 0, "Нет ресурсов модальности %r" % modality


def test_resource_features_catalog_not_empty(sql):
    """core-api при старте грузит отсюда допустимые признаки; пусто → валидация признаков молча выключена."""
    (n,), = sql("select count(*) from eco_assistant.resource_feature")
    assert n > 0, "eco_assistant.resource_feature пуст"


@pytest.mark.parametrize("name", KNOWN_OBJECTS)
def test_known_object_has_resources(sql, name):
    rows = sql("""
        select o.db_id, count(ro.resource_id)
        from eco_assistant.object_name_synonym s
        join eco_assistant.object_name_synonym_link l on l.synonym_id = s.id
        join eco_assistant.object o on o.id = l.object_id
        left join eco_assistant.resource_object ro on ro.object_id = o.id
        where lower(s.synonym) = lower(%s)
        group by o.db_id""", (name,))
    assert rows, "Объект %r не найден по синонимам" % name
    assert any(cnt > 0 for _, cnt in rows), "У объекта %r нет ни одного ресурса: %s" % (name, rows)


def test_resource_value_points_to_existing_resource(sql):
    """resource_value.resource_id объявлен без внешнего ключа — висячие ссылки возможны."""
    (n,), = sql("""select count(*) from eco_assistant.resource_value rv
                   left join eco_assistant.resource r on r.id = rv.resource_id where r.id is null""")
    assert n == 0, "%d строк resource_value ссылаются на несуществующий resource" % n


def test_resource_value_points_to_existing_value(sql):
    """value_id ссылается в таблицу из modality.value_table_name — тоже без внешнего ключа."""
    problems = []
    for modality, table in sql("select modality_type, value_table_name from eco_assistant.modality"):
        if not sql("select to_regclass(%s)", ("eco_assistant." + table,))[0][0]:
            problems.append("%s: таблицы %s нет" % (modality, table))
            continue
        (n,), = sql("""select count(*) from eco_assistant.resource_value rv
                       join eco_assistant.modality m on m.id = rv.modality_id
                       left join eco_assistant.{t} v on v.id = rv.value_id
                       where m.modality_type = %s and rv.value_id is not null and v.id is null""".format(t=table),
                    (modality,))
        if n:
            problems.append("%s: %d висячих value_id → %s" % (modality, n, table))
    assert not problems, "\n".join(problems)


def test_image_urls_have_no_template_placeholder(sql):
    """resources.json содержит {{PUBLIC_BASE_URL}} — init_db.sh должен был его подставить."""
    (n,), = sql("""select count(*) from eco_assistant.image_value
                   where file_path like '%%{{%%' or url like '%%{{%%'""")
    assert n == 0, "%d картинок с неподставленным {{PUBLIC_BASE_URL}} в адресе" % n


def test_image_paths_are_absolute_urls(sql):
    rows = sql("""select file_path from eco_assistant.image_value
                  where file_path is not null and file_path !~ '^https?://' limit 5""")
    assert not rows, "Пути к фото не являются URL (браузер их не откроет): %s" % [r[0] for r in rows]


def test_map_resources_have_geometry(sql):
    """Импортёр берёт геометрии из json_files/geodb.json, а он в salut_bot/.gitignore — на чистом клоне
    его нет, и все ресурсы «Картографическая информация» импортируются без геометрии (value_id = NULL)."""
    (total, empty), = sql("""select count(*), count(*) filter (where rv.value_id is null)
                             from eco_assistant.resource_value rv
                             join eco_assistant.modality m on m.id = rv.modality_id
                             where m.modality_type = 'Геоданные'""")
    assert total > 0, "Нет ни одного картографического ресурса"
    assert empty == 0, ("%d из %d картографических ресурсов без геометрии — карты и поиск по области "
                        "работать не будут. Нужен geodb.json при импорте (или --ensure-geometries)." % (empty, total))


def test_geodata_srid_4326(sql):
    (n,), = sql("select count(*) from eco_assistant.geodata_value where ST_SRID(geometry) <> 4326")
    assert n == 0, "%d геообъектов не в SRID 4326" % n


def test_geodata_mostly_valid(sql):
    (total, invalid), = sql("""select count(*), count(*) filter (where not ST_IsValid(geometry))
                               from eco_assistant.geodata_value""")
    if total == 0:
        pytest.skip("геометрий нет вообще — см. test_map_resources_have_geometry")
    share = invalid / total
    assert share <= 0.01, "%.1f%% геометрий невалидны (%d из %d) — поиск по полигонам будет врать" % (
        share * 100, invalid, total)
