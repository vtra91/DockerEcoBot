"""Доступ к стеку docker compose из контейнера с тестами (через /var/run/docker.sock)."""
import json
import os
import re
import socket
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import docker

PROBE_SRC = (Path(__file__).parent / "probe.py").read_text(encoding="utf-8")


def env_list(name, default=""):
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# Сервисы, без которых система не работает
CORE_SERVICES = env_list("HEALTH_SERVICES", "redis,db,backend,core-api,max,admin,nginx,dsapi,stt-service")
# Сервисы под профилями: проверяются, только если запущены
OPTIONAL_SERVICES = env_list("HEALTH_OPTIONAL_SERVICES", "annotator")

# Адреса сервисов внутри ecobot_net (порт — тот, что слушает процесс в контейнере)
INTERNAL_URLS = {
    "backend": "http://backend:8000",
    "core-api": "http://core-api:5001",
    "admin": "http://admin:8000",
    "dsapi": "http://dsapi:5050",
    "stt-service": "http://stt-service:8081",
    "annotator": "http://annotator:8010",
    "max": "http://max:8080",
    "nginx": "http://nginx:80",
}


def internal_url(service):
    override = os.getenv("HEALTH_URL_" + re.sub(r"\W", "_", service).upper())
    return (override or INTERNAL_URLS[service]).rstrip("/")


def normalize_base_path(value):
    value = (value or "").strip().strip("/")
    return "/" + value if value else ""


def _parse_docker_time(value):
    # 2026-11-19T10:12:04.123456789Z → datetime (наносекунды python не умеет)
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?", value)
    frac = (m.group(2) or ".0")[:7]
    return datetime.strptime(m.group(1) + frac, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)


class Stack:
    def __init__(self):
        self.client = docker.from_env()
        self.project = self._detect_project()

    def _detect_project(self):
        explicit = os.getenv("HEALTH_COMPOSE_PROJECT")
        if explicit:
            return explicit
        # Мы сами запущены как `docker compose run health-tests` — берём проект со своей метки
        try:
            me = self.client.containers.get(socket.gethostname())
            return me.labels["com.docker.compose.project"]
        except Exception:
            pass
        projects = {c.labels.get("com.docker.compose.project")
                    for c in self.client.containers.list(filters={"label": "com.docker.compose.service=backend"})}
        projects.discard(None)
        if len(projects) == 1:
            return projects.pop()
        raise RuntimeError("Не удалось определить compose-проект, задайте HEALTH_COMPOSE_PROJECT "
                           "(найдены: %s)" % (sorted(projects) or "ничего"))

    # --- контейнеры -------------------------------------------------------
    @lru_cache(maxsize=None)
    def container(self, service):
        """Контейнер сервиса (предпочтительно запущенный, не одноразовый `run`)."""
        found = self.client.containers.list(all=True, filters={
            "label": ["com.docker.compose.project=%s" % self.project,
                      "com.docker.compose.service=%s" % service]})
        found = [c for c in found if c.labels.get("com.docker.compose.oneoff", "False") != "True"]
        if not found:
            return None
        found.sort(key=lambda c: c.attrs["Created"], reverse=True)   # самый свежий…
        found.sort(key=lambda c: c.status != "running")              # …и запущенный — первым
        return found[0]

    def is_running(self, service):
        c = self.container(service)
        return c is not None and c.status == "running"

    def services_running(self):
        found = self.client.containers.list(filters={"label": "com.docker.compose.project=%s" % self.project})
        return sorted({c.labels["com.docker.compose.service"] for c in found
                       if c.labels.get("com.docker.compose.oneoff", "False") != "True"})

    @lru_cache(maxsize=None)
    def env(self, service):
        """Итоговое окружение контейнера (env_file + environment), как его видит процесс."""
        c = self.container(service)
        if c is None:
            return {}
        result = {}
        for item in c.attrs["Config"].get("Env") or []:
            key, _, value = item.partition("=")
            result[key] = value
        return result

    def started_at(self, service):
        return _parse_docker_time(self.container(service).attrs["State"]["StartedAt"])

    def logs_since_start(self, service):
        c = self.container(service)
        raw = c.logs(since=self.started_at(service), stdout=True, stderr=True)
        return raw.decode("utf-8", "replace")

    # --- выполнение внутри контейнера ------------------------------------
    def exec(self, service, cmd, timeout_note=""):
        c = self.container(service)
        if c is None or c.status != "running":
            raise RuntimeError("сервис %s не запущен — exec невозможен" % service)
        code, out = c.exec_run(cmd, demux=False)
        return code, (out or b"").decode("utf-8", "replace")

    @lru_cache(maxsize=None)
    def _probe_cached(self, service, specs_json):
        code, out = self.exec(service, ["python", "-c", PROBE_SRC, specs_json])
        for line in out.splitlines():
            if line.startswith("__PROBE__"):
                return {r["name"]: r for r in json.loads(line[len("__PROBE__"):])}
        raise RuntimeError("проба в %s не отработала (exit %s):\n%s" % (service, code, out[-2000:]))

    def probe(self, service, specs):
        """Прогоняет список проверок одним exec и возвращает {name: result}."""
        return self._probe_cached(service, json.dumps(specs, ensure_ascii=False, sort_keys=True))
