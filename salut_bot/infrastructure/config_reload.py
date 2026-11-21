""" Горячее перечитывание LLM-конфига из shared.env без пересоздания контейнера."""

import logging
import os
from pathlib import Path

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

SHARED_ENV_PATH = Path("/app/shared.env")

# Что можно менять на лету. Остальное (DB_*, REDIS_*, ECOBOT_API_BASE_URL,
# токены ботов, пути) читается один раз при старте и требует пересоздания.
RELOADABLE_PREFIXES = ("LLM_", "GIGACHAT_")
RELOADABLE_KEYS = frozenset({
    "SBER_KEY_ENTERPRICE",
    "PROMO_ENABLED",
    "PROMO_RELATION_TYPE",
})

# Ключи, значения которых нельзя возвращать в ответе и писать в лог.
_SECRET_HINTS = ("KEY", "CREDENTIALS", "TOKEN", "SECRET", "PASSWORD")


def is_reloadable(key: str) -> bool:
    """ Можно ли применять этот ключ без пересоздания контейнера."""
    return key.startswith(RELOADABLE_PREFIXES) or key in RELOADABLE_KEYS


def _mask(key: str, value: str) -> str:
    """ Скрывает секреты, чтобы они не утекли в лог и в ответ админки."""
    if any(hint in key.upper() for hint in _SECRET_HINTS):
        return "***"
    return value


def apply_shared_env(path=None) -> dict:
    """ Перечитывает shared.env в os.environ работающего процесса.
    Возвращает отчёт (applied / unchanged / skipped / error) для админки.
    """
    env_path = Path(path) if path is not None else SHARED_ENV_PATH

    report = {
        "path": str(env_path),
        "exists": False,
        "applied": {},
        "unchanged": [],
        "skipped": [],
        "error": None,
    }

    if not env_path.exists():
        # Самая частая причина: volume не добавлен в docker-compose.yml.
        report["error"] = (
            f"{env_path} не найден внутри контейнера — проверьте volume "
            f"'./shared.env:{env_path}:ro' в docker-compose.yml"
        )
        logger.error("[ConfigReload] %s", report["error"])
        return report

    report["exists"] = True

    try:
        values = dotenv_values(str(env_path))
    except Exception as exc:
        report["error"] = f"не удалось прочитать {env_path}: {exc}"
        logger.error("[ConfigReload] %s", report["error"])
        return report

    for key, value in values.items():
        if value is None:
            continue
        if not is_reloadable(key):
            report["skipped"].append(key)
            continue
        if os.environ.get(key) == value:
            report["unchanged"].append(key)
            continue
        os.environ[key] = value
        report["applied"][key] = _mask(key, value)

    if report["applied"]:
        logger.info("[ConfigReload] Применены ключи: %s", ", ".join(sorted(report["applied"])))
    else:
        logger.info("[ConfigReload] Изменений в LLM-конфиге нет")

    return report