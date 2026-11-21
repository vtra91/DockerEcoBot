#!/usr/bin/env python3
"""
Безопасная замена префикса домена в eco_assistant.image_value.file_path.

Что делает:
  1. Спрашивает старый и новый префикс.
  2. Показывает количество и примеры затрагиваемых строк.
  3. Делает резервную копию таблицы image_value (и, если есть pg_dump, полный дамп БД).
  4. В транзакции меняет только начало file_path.
  5. Проверяет, что старый префикс не остался.

Запуск внутри backend-контейнера:
  docker cp scripts/replace_image_domain.py dockerecobot-backend-1:/tmp/
  docker exec -it dockerecobot-backend-1 python /tmp/replace_image_domain.py

Запуск на хосте (нужен psycopg2 и доступ к PostgreSQL):
  set DB_HOST=localhost
  python scripts/replace_image_domain.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    print("Ошибка: не установлен psycopg2.")
    print("Запустите скрипт внутри backend-контейнера или установите psycopg2.")
    sys.exit(1)


TABLE = "eco_assistant.image_value"
_DEFAULT_BACKUP_DIR = "/app/logs/backups" if Path("/app/logs").exists() else "backups"
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", _DEFAULT_BACKUP_DIR))


def ask_prefix(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if not value:
            print("Пустое значение, попробуйте ещё раз.")
            continue

        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            print("Нужен полный URL, например: https://old.example.com")
            continue

        if parsed.query or parsed.fragment:
            print("URL не должен содержать ?query или #fragment.")
            continue

        return value.rstrip("/")


def _default_db_host() -> str:
    # Внутри контейнера backend БД доступна по имени db,
    # на хосте — по localhost через опубликованный порт 5432.
    return "db" if Path("/.dockerenv").exists() else "localhost"


def db_params() -> dict:
    return {
        "host": os.getenv("DB_HOST", _default_db_host()),
        "port": os.getenv("DB_PORT", "5432"),
        "dbname": os.getenv("DB_NAME", "eco"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", "changeme"),
        "connect_timeout": 5,
    }


def connect_db():
    params = db_params()
    print(
        f"Подключение: {params['user']}@{params['host']}:{params['port']}/"
        f"{params['dbname']}"
    )
    try:
        return psycopg2.connect(**params)
    except UnicodeDecodeError:
        print("\nНе удалось подключиться к PostgreSQL.")
        print("Похоже, сервер вернул сообщение об ошибке в CP1251, а psycopg2")
        print("ожидает UTF-8. Обычно причина — неверный DB_HOST.")
        print("На хосте укажите DB_HOST=localhost, в контейнере — DB_HOST=db.")
        raise SystemExit(4)
    except psycopg2.OperationalError as exc:
        print(f"\nОшибка подключения к PostgreSQL: {exc}")
        print("Проверьте DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.")
        raise SystemExit(4)


def try_pg_dump(timestamp: str) -> Path | None:
    """Полный дамп БД, если pg_dump доступен. Иначе None."""
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        print("pg_dump не найден — будет только бэкап таблицы image_value.")
        return None

    params = db_params()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = BACKUP_DIR / f"eco_{timestamp}.dump"

    env = os.environ.copy()
    env["PGPASSWORD"] = params["password"]

    cmd = [
        pg_dump,
        "-Fc",
        "-h", params["host"],
        "-p", str(params["port"]),
        "-U", params["user"],
        "-d", params["dbname"],
        "-f", str(dump_path),
    ]
    print(f"Полный дамп БД: {dump_path}")
    subprocess.run(cmd, env=env, check=True)
    return dump_path


def main() -> int:
    print("=" * 70)
    print("Замена домена в eco_assistant.image_value.file_path")
    print("=" * 70)

    old = ask_prefix("Старый префикс (что заменяем)")
    new = ask_prefix("Новый префикс (на что заменяем)")

    if old == new:
        print("Старый и новый префикс совпадают — менять нечего.")
        return 1

    conn = connect_db()
    conn.autocommit = False
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {TABLE} "
                "WHERE left(file_path, %s) = %s",
                (len(old), old),
            )
            total = cur.fetchone()[0]

            print(f"\nСтрок с префиксом {old!r}: {total}")
            if total == 0:
                print("Нечего менять.")
                return 1

            cur.execute(
                f"SELECT id, file_path FROM {TABLE} "
                "WHERE left(file_path, %s) = %s "
                "ORDER BY id LIMIT 5",
                (len(old), old),
            )
            print("\nПримеры:")
            for row_id, file_path in cur.fetchall():
                new_path = new + file_path[len(old):]
                print(f"  id={row_id}")
                print(f"    было:  {file_path}")
                print(f"    станет:{new_path}")

            print(
                "\nБудет изменено только начало file_path. "
                "Поле url (внешний источник) не трогаем."
            )
            answer = input("\nПродолжить? Введите yes: ").strip().lower()
            if answer not in ("yes", "y", "да"):
                print("Отменено.")
                return 0

            dump_path = try_pg_dump(timestamp)

            # Бэкап таблицы — всегда, даже если pg_dump недоступен.
            backup_table = f"image_value_backup_{timestamp}"
            cur.execute(
                sql.SQL("CREATE TABLE eco_assistant.{} AS TABLE eco_assistant.image_value").format(
                    sql.Identifier(backup_table)
                )
            )
            conn.commit()
            print(f"Бэкап таблицы: eco_assistant.{backup_table}")

            # CSV-бэкап всех строк таблицы.
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            csv_path = BACKUP_DIR / f"image_value_{timestamp}.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                cur.copy_expert(
                    "COPY (SELECT id, url, file_path, format "
                    "FROM eco_assistant.image_value ORDER BY id) "
                    "TO STDOUT WITH CSV HEADER",
                    f,
                )
            conn.commit()
            print(f"CSV-бэкап: {csv_path}")

            # Блокируем таблицу на время замены, чтобы не было гонок.
            cur.execute(f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE")

            cur.execute(
                f"SELECT count(*) FROM {TABLE} "
                "WHERE left(file_path, %s) = %s",
                (len(old), old),
            )
            expected = cur.fetchone()[0]

            cur.execute(
                f"UPDATE {TABLE} "
                "SET file_path = %s || substr(file_path, %s), "
                "    updated_at = now() "
                "WHERE left(file_path, %s) = %s",
                (new, len(old) + 1, len(old), old),
            )
            updated = cur.rowcount

            cur.execute(
                f"SELECT count(*) FROM {TABLE} "
                "WHERE left(file_path, %s) = %s",
                (len(old), old),
            )
            left_old = cur.fetchone()[0]

            cur.execute(
                f"SELECT count(*) FROM {TABLE} "
                "WHERE left(file_path, %s) = %s",
                (len(new), new),
            )
            new_count = cur.fetchone()[0]

            print("\nРезультат:")
            print(f"  ожидалось строк: {expected}")
            print(f"  обновлено:       {updated}")
            print(f"  осталось старых: {left_old}")
            print(f"  стало с новым:   {new_count}")

            if updated != expected or left_old != 0:
                conn.rollback()
                print("\nПроверка не прошла — изменения откатаны.")
                return 2

            conn.commit()
            print("\nГотово. Изменения зафиксированы.")
            if dump_path:
                print(f"Полный дамп: {dump_path}")
            print(f"Бэкап таблицы: eco_assistant.{backup_table}")
            print(f"CSV: {csv_path}")
            return 0

    except Exception as exc:
        conn.rollback()
        print(f"\nОшибка: {exc}")
        print("Изменения откатаны.")
        return 3
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
