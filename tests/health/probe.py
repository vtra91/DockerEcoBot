"""
Проба, которая выполняется ВНУТРИ контейнера сервиса (docker exec ... python -c <этот файл> <spec>).

Зачем изнутри: так проверяется ровно то, что видит сам сервис — его DNS, его сеть,
его переменные окружения, его драйверы БД, права его пользователя на тома.
Проверка «из соседнего контейнера» этого не ловит (например, пароль БД в .env
админки другой, чем у backend).

Только стандартная библиотека: файл должен работать в любом python-образе (3.8+).
Строки вида ${VAR} / ${VAR:-default} в спецификации раскрываются из окружения контейнера.

Вывод: одна строка "__PROBE__<json>" со списком результатов
[{"name": ..., "ok": bool, "skipped": bool, "detail": str}, ...]
"""
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

_VAR = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def expand(value):
    if not isinstance(value, str):
        return value
    return _VAR.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)


def _tcp(host, port, timeout):
    started = time.time()
    with socket.create_connection((host, int(port)), timeout=timeout):
        pass
    return "%s:%s открыт (%.0f мс)" % (host, port, (time.time() - started) * 1000)


def check_tcp(spec, timeout):
    return True, _tcp(expand(spec["host"]), expand(spec["port"]), timeout)


def check_redis(spec, timeout):
    host, port = expand(spec["host"]), int(expand(spec["port"]))
    with socket.create_connection((host, port), timeout=timeout) as sock:
        password = expand(spec.get("password", ""))
        if password:
            sock.sendall(("AUTH %s\r\n" % password).encode())
            sock.recv(64)
        sock.sendall(b"PING\r\n")
        reply = sock.recv(64)
    if reply.startswith(b"+PONG"):
        return True, "%s:%s PONG" % (host, port)
    return False, "%s:%s ответил %r вместо +PONG" % (host, port, reply[:40])


def check_pg(spec, timeout):
    params = {k: expand(spec[k]) for k in ("host", "port", "db", "user", "password")}
    if not params["password"]:
        return False, "пароль БД пустой (переменная из спецификации не задана в окружении контейнера)"
    query = "select current_database(), current_user, version()"
    errors = []
    try:
        import psycopg2  # noqa
        conn = psycopg2.connect(host=params["host"], port=int(params["port"]), dbname=params["db"],
                                user=params["user"], password=params["password"],
                                connect_timeout=int(timeout))
        try:
            cur = conn.cursor()
            cur.execute(query)
            row = cur.fetchone()
        finally:
            conn.close()
        return True, "psycopg2: db=%s user=%s" % (row[0], row[1])
    except ImportError:
        errors.append("psycopg2 нет")
    except Exception as exc:  # реальная ошибка подключения — дальше не пробуем
        return False, "psycopg2: %s: %s" % (type(exc).__name__, str(exc).strip())
    try:
        import asyncio
        import asyncpg  # noqa

        async def run():
            conn = await asyncpg.connect(host=params["host"], port=int(params["port"]), database=params["db"],
                                         user=params["user"], password=params["password"], timeout=timeout)
            try:
                return await conn.fetchrow(query)
            finally:
                await conn.close()

        row = asyncio.run(run())
        return True, "asyncpg: db=%s user=%s" % (row[0], row[1])
    except ImportError:
        errors.append("asyncpg нет")
    except Exception as exc:
        return False, "asyncpg: %s: %s" % (type(exc).__name__, str(exc).strip())
    # Драйвера нет — хотя бы порт
    return True, "драйвера БД нет (%s); %s" % (", ".join(errors), _tcp(params["host"], params["port"], timeout))


def check_http(spec, timeout):
    url = expand(spec["url"])
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False, "адрес %s после подстановки = %r — переменная не задана в окружении контейнера" % (
            spec["url"], url)
    headers = {k: expand(v) for k, v in (spec.get("headers") or {}).items()}
    req = urllib.request.Request(url, method=spec.get("method", "GET"), headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        status, body = resp.status, resp.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(4096)
    text = body.decode("utf-8", "replace")
    expect = spec.get("expect", "any")
    if expect != "any" and status not in expect:
        return False, "%s → HTTP %s (ожидали %s): %s" % (url, status, expect, text[:200].strip())
    must = expand(spec.get("body_contains", ""))
    if must and must not in text:
        return False, "%s → HTTP %s, но в ответе нет %r" % (url, status, must)
    return True, "%s → HTTP %s" % (url, status)


def check_path(spec, timeout):
    path = expand(spec["path"])
    kind = spec["expect"]
    if kind == "file":
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return True, "%s (%d байт)" % (path, os.path.getsize(path))
        return False, "%s: файла нет или он пустой" % path
    if kind == "dir_nonempty":
        if not os.path.isdir(path):
            return False, "%s: каталога нет" % path
        entries = os.listdir(path)
        if not entries:
            return False, "%s: каталог пустой" % path
        return True, "%s: %d элементов" % (path, len(entries))
    if kind == "writable":
        target = path if os.path.isdir(path) else os.path.dirname(path)
        if not os.path.isdir(target):
            return False, "%s: каталога нет" % target
        if os.path.isfile(path):
            if os.access(path, os.W_OK):
                return True, "%s доступен на запись (uid=%s)" % (path, os.getuid())
            return False, "%s: нет прав на запись (uid=%s)" % (path, os.getuid())
        with tempfile.NamedTemporaryFile(dir=target, prefix=".healthcheck-"):
            pass
        return True, "%s доступен на запись (uid=%s)" % (target, os.getuid())
    return False, "неизвестный тип проверки пути: %s" % kind


def check_python(spec, timeout):
    """Произвольный фрагмент: должен положить в переменную result пару (ok, detail)."""
    scope = {"expand": expand, "os": os, "json": json}
    exec(spec["code"], scope)  # noqa: S102 — код наш, из tests/health
    ok, detail = scope["result"]
    return bool(ok), str(detail)


CHECKS = {"tcp": check_tcp, "redis": check_redis, "pg": check_pg, "http": check_http,
          "path": check_path, "python": check_python}


def main():
    specs = json.loads(sys.argv[1])
    results = []
    for spec in specs:
        timeout = float(spec.get("timeout", 10))
        skip_unless = spec.get("skip_unless_env")
        if skip_unless and not os.environ.get(skip_unless):
            results.append({"name": spec["name"], "ok": True, "skipped": True,
                            "detail": "переменная %s не задана — проверка не применима" % skip_unless})
            continue
        try:
            ok, detail = CHECKS[spec["type"]](spec, timeout)
        except Exception as exc:
            target = expand(spec.get("url") or spec.get("path") or
                            ("%s:%s" % (spec.get("host"), spec.get("port")) if spec.get("host") else ""))
            ok, detail = False, "%s%s: %s" % (target + " → " if target else "", type(exc).__name__,
                                              str(exc).strip() or repr(exc))
        results.append({"name": spec["name"], "ok": ok, "skipped": False, "detail": detail})
    sys.stdout.write("__PROBE__" + json.dumps(results, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
