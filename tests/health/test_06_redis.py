"""Уровень 5: Redis как шина между сервисами.

Раскладка по базам (из кода):
  db0 — настройки из админки (settings:*), история диалогов, канал config:reload (admin/core-api → max)
  db1 — кэш поиска backend (REDIS_DB по умолчанию 1)
  db2 — heartbeat бота (ключ baikal_bot_last_seen, читает админка)
"""


def test_redis_ping(redis_db):
    assert redis_db(0).ping()


def test_admin_initialized_settings(redis_db):
    """Админка при старте кладёт settings:* с nx=True — если ключей нет, админка смотрит в другой Redis/БД
    или не стартовала."""
    r = redis_db(0)
    missing = [k for k in ("settings:promo_enabled", "settings:multi_act_dialog") if r.get(k) is None]
    assert not missing, "В Redis db0 нет %s" % missing


def test_max_listens_config_reload(redis_db, require):
    """max подписан на config:reload — иначе «Сохранить» в настройках админки не применяется к боту без рестарта."""
    require("max")
    (_, subscribers), = redis_db(0).pubsub_numsub("config:reload")
    assert subscribers >= 1, "На канал config:reload никто не подписан — max не слушает перезагрузку конфига"


def test_bot_heartbeat_present(redis_db, require):
    """Индикатор «бот онлайн» в админке = наличие ключа baikal_bot_last_seen (TTL 120 с) в db2."""
    require("max")
    value = redis_db(2).get("baikal_bot_last_seen")
    assert value, ("Ключа heartbeat нет — админка всегда покажет «бот offline». "
                   "BotHeartbeat.ping() есть в utils/heartbeat.py, но в main_max.py его никто не вызывает.")
