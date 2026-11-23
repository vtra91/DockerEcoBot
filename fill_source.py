#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fill_sources.py — читает merged.json, собирает уникальные страницы
и добавляет BibTeX-ссылки в source.name.
"""
import os
import json
import argparse
import getpass
import psycopg2

SCHEMA = "eco_assistant"
DSN = "host=db port=5432 dbname=eco user=postgres"

# Данные книги (общие для всех страниц)
BIB_BOOK = {
    "booktitle": "Атлас пресноводных рыб России. Т. 1",
    "editor": "Решетников, Ю. С.",
    "edition": "2-е изд.",
    "publisher": "Наука",
    "address": "Москва",
    "year": "2003",
    "isbn": "5-02-002871-1",
    "note": "379 с.",
    "language": "russian",
}

def make_key(chapter):
    """Генерирует уникальный ключ для BibTeX."""
    w = chapter.split()
    name_part = "".join(w[:2]) if len(w) > 1 else w[0]
    return f"atlas{BIB_BOOK['year']}_{name_part}".lower()

def build_bibtex(chapter, page):
    """Формирует строку BibTeX."""
    chapter_clean = chapter.replace("{", "").replace("}", "").replace("—", "-")
    fields = {"title": chapter_clean, **BIB_BOOK, "pages": str(page)}
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items())
    return f"@incollection{{{make_key(chapter_clean)},\n{body}\n}}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="merged.json", help="Путь к JSON-файлу")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--print", dest="show", action="store_true")
    args = ap.parse_args()

    # 1. Читаем JSON
    with open(args.file, encoding="utf-8-sig") as f:
        fishes = json.load(f)

    # 2. Собираем уникальные страницы
    # Словарь: {номер_страницы: название_главы}
    pages_to_process = {}
    for fish in fishes:
        page = fish.get("номер страницы, где описание")
        if page is None:
            continue
        page = int(page)
        
        # Берем название на русском, если нет — латинское
        name = fish.get("название на русском") or fish.get("латинское название")
        if not name:
            continue
            
        # Если страницы еще нет в словаре 
        if page not in pages_to_process:
            pages_to_process[page] = name
            
    print(f"Найдено уникальных страниц в JSON: {len(pages_to_process)}")

    # Подключение к базе
    pwd = os.environ.get("PGPASSWORD") or getpass.getpass("Пароль PostgreSQL: ")
    conn = psycopg2.connect(DSN, password=pwd)
    conn.autocommit = False
    cur = conn.cursor()

    st = {"total_pages": len(pages_to_process), "inserted": 0, "existing": 0, "errors": []}

    try:
        for page, chapter in sorted(pages_to_process.items()):
            name = build_bibtex(chapter, page)
            
            cur.execute("SAVEPOINT r")
            try:
                cur.execute(f"SELECT id FROM {SCHEMA}.source WHERE name = %s ORDER BY id LIMIT 1", (name,))
                if cur.fetchone():
                    st["existing"] += 1
                else:
                    cur.execute(f"INSERT INTO {SCHEMA}.source (name) VALUES (%s)", (name,))
                    st["inserted"] += 1
                    
                if args.show:
                    print(f"Стр. {page}: {chapter}")
                    
                cur.execute("RELEASE SAVEPOINT r")
            except Exception as e:
                cur.execute("ROLLBACK TO SAVEPOINT r")
                cur.execute("RELEASE SAVEPOINT r")
                st["errors"].append(f"стр.{page}: {e}")

        if args.dry_run:
            conn.rollback()
            print("dry-run: изменения отменены")
        else:
            conn.commit()
            print("COMMIT")
        print(json.dumps(st, ensure_ascii=False, indent=2))
        
    except Exception as e:
        conn.rollback()
        print("ОШИБКА, ROLLBACK:", e)
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()