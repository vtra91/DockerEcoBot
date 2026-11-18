#!/usr/bin/env python3
"""
Автоматический патчер проекта DockerEcoBot под BASE_PATH=/ecoassistant из shared.env.
Исправляет редиректы, двойной префикс и ошибку отсутствия таблицы error_log в БД.
"""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent

ADMIN_MAIN_PATCH = """
# === START ECOASSISTANT BASE_PATH PATCH ===
BASE_PATH = os.getenv("BASE_PATH", "/ecoassistant").rstrip("/")

def _eco_admin_url(url: str) -> str:
    u = str(url).strip()
    if u.startswith(("http://", "https://")):
        return u
    bp = os.getenv("BASE_PATH", "/ecoassistant").rstrip("/")
    # Срезаем любые уже имеющиеся префиксы /ecoassistant и /admin, чтобы собрать чистый путь ровно 1 раз
    while True:
        if bp and u.startswith(bp + "/"):
            u = u[len(bp):]
        elif bp and u == bp:
            u = "/"
        elif u.startswith("/admin/"):
            u = u[len("/admin"):]
        elif u == "/admin":
            u = "/"
        else:
            break
    if not u.startswith("/"):
        u = "/" + u
    return f"{bp}/admin{u}" if bp else f"/admin{u}"

_OrigRedirectResponse = RedirectResponse
class RedirectResponse(_OrigRedirectResponse):
    def __init__(self, url, *args, **kwargs):
        super().__init__(url=_eco_admin_url(url), *args, **kwargs)
# === END ECOASSISTANT BASE_PATH PATCH ===
"""

JS_INTERCEPTOR = """
<script id="ecoassistant-basepath-patch">
(function() {
    var bp = "{{ base_path | default('/ecoassistant') }}".replace(/\\/+$/, "");
    if (!bp || bp.indexOf("{") !== -1) bp = "/ecoassistant";
    window.BASE_PATH = bp;

    function fixUrl(u) {
        if (typeof u !== "string") return u;
        while (u.indexOf(bp + bp + "/") === 0) {
            u = u.slice(bp.length);
        }
        if (u.startsWith("/") && !u.startsWith("//") && !u.startsWith(bp + "/") && u !== bp) {
            return bp + u;
        }
        return u;
    }

    var origFetch = window.fetch;
    if (origFetch) {
        window.fetch = function(input, init) {
            if (typeof input === "string") {
                input = fixUrl(input);
            } else if (input && typeof input.url === "string") {
                input = new Request(fixUrl(input.url), input);
            }
            return origFetch.call(this, input, init);
        };
    }

    var origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        arguments[1] = fixUrl(url);
        return origOpen.apply(this, arguments);
    };
})();
</script>
"""


def patch_admin_panel():
    admin_dir = ROOT / "AdminPanel"
    if not admin_dir.exists():
        print(f"[ERROR] Директория {admin_dir} не найдена!")
        return

    main_py = admin_dir / "main.py"
    if main_py.exists():
        content = main_py.read_text(encoding="utf-8", errors="ignore")
        original = content

        # 1. Заменяем старый блок патча (если был) на новый
        if "# === START ECOASSISTANT BASE_PATH PATCH ===" in content:
            content = re.sub(
                r"# === START ECOASSISTANT BASE_PATH PATCH ===.*?# === END ECOASSISTANT BASE_PATH PATCH ===",
                ADMIN_MAIN_PATCH.strip(),
                content,
                flags=re.DOTALL,
            )
        else:
            content = content.replace("load_dotenv()", "load_dotenv()\n" + ADMIN_MAIN_PATCH, 1)

        # 2. Прокидываем base_path в Jinja2 globals
        if 'templates.env.globals["base_path"]' not in content and "templates = Jinja2Templates" in content:
            content = content.replace(
                'templates = Jinja2Templates(directory="templates")',
                'templates = Jinja2Templates(directory="templates")\n'
                'templates.env.globals["base_path"] = os.getenv("BASE_PATH", "/ecoassistant").rstrip("/")',
                1,
            )

        # 3. Защищаем запрос к error_log в dashboard() от падения, если таблица ещё не создана
        old_err_query = (
            "    q = select(func.count(ErrorLog.id)).where(ErrorLog.created_at >= time_24h_ago)\n"
            "    errors_24h = (await db.execute(q)).scalar() or 0"
        )
        new_err_query = (
            "    try:\n"
            "        q = select(func.count(ErrorLog.id)).where(ErrorLog.created_at >= time_24h_ago)\n"
            "        errors_24h = (await db.execute(q)).scalar() or 0\n"
            "    except Exception as _e:\n"
            "        await db.rollback()\n"
            "        errors_24h = 0"
        )
        if old_err_query in content:
            content = content.replace(old_err_query, new_err_query, 1)

        # 4. Защищаем /logs от падения, если error_log ещё нет
        old_logs_query = (
            "    query = select(ErrorLog).order_by(ErrorLog.created_at.desc()).limit(50)\n"
            "    result = await db.execute(query)\n"
            "    errors = result.scalars().all()"
        )
        new_logs_query = (
            "    try:\n"
            "        query = select(ErrorLog).order_by(ErrorLog.created_at.desc()).limit(50)\n"
            "        result = await db.execute(query)\n"
            "        errors = result.scalars().all()\n"
            "    except Exception:\n"
            "        await db.rollback()\n"
            "        errors = []"
        )
        if old_logs_query in content:
            content = content.replace(old_logs_query, new_logs_query, 1)

        if content != original:
            main_py.write_text(content, encoding="utf-8")
            print("[PATCHED] AdminPanel/main.py")

    # Патчим HTML-шаблоны
    templates_dir = admin_dir / "templates"
    if templates_dir.exists():
        for html_file in templates_dir.rglob("*.html"):
            if html_file.name.endswith((".bak2", ".bak3", ".bak4", ".bak5")):
                continue
            content = html_file.read_text(encoding="utf-8", errors="ignore")
            original = content

            if html_file.name in ("base.html", "login.html") and 'id="ecoassistant-basepath-patch"' not in content:
                if "</head>" in content:
                    content = content.replace("</head>", JS_INTERCEPTOR + "\n</head>", 1)

            # Приводим все ссылки к единому виду {{ base_path }}/admin...
            content = content.replace("/ecoassistant/ecoassistant", "/ecoassistant")
            content = content.replace("{{ base_path }}/ecoassistant", "{{ base_path }}")
            content = content.replace("{{ base_path }}{{ base_path }}", "{{ base_path }}")
            content = re.sub(r'(href|src|action)=(["\'])/admin\b', r'\1=\2{{ base_path }}/admin', content)
            content = content.replace("{{ base_path }}{{ base_path }}", "{{ base_path }}")

            if content != original:
                html_file.write_text(content, encoding="utf-8")
                print(f"[PATCHED] {html_file.relative_to(ROOT)}")


def main():
    patch_admin_panel()
    print("=== Патчинг завершён успешно ===")


if __name__ == "__main__":
    main()
