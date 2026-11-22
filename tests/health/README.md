# Тесты работоспособности стека

Проверяют уже запущенный стек (`docker compose up -d`): что каждый сервис жив, что связи между
сервисами есть, что модели/индексы/данные на месте и что наружу торчит только то, что должно.

```bash
bash scripts/healthcheck.sh                 # всё (~1–5 мин, первый прогон дольше — сборка образа)
bash scripts/healthcheck.sh -m "not slow"   # быстро: без LLM и whisper
bash scripts/healthcheck.sh -k database     # выборочно
bash scripts/healthcheck.sh -x              # остановиться на первой ошибке
```

Тесты идут в отдельном контейнере `health-tests` (профиль `health`) в сети `ecobot_net`
и через `/var/run/docker.sock` читают окружение и логи контейнеров и запускают пробы **внутри** сервисов.
Ничего не меняют: БД открывается только на чтение, POST-запросы — только безопасные.

## Что проверяется

| Файл | Что |
|---|---|
| `test_01_containers` | все контейнеры `running`/`healthy`, нет перезапусков по кругу, в логах с момента старта нет Traceback/ImportError/FATAL |
| `test_02_config` | реальное окружение контейнеров согласовано: параметры БД, `BASE_PATH`, `PUBLIC_BASE_URL`, токены admin↔dsapi↔annotator, нет секретов из `.env.example`, все внутренние URL в env ведут в живые сервисы |
| `test_03_connectivity` | изнутри каждого сервиса, с его переменными и драйвером: backend/core-api/admin → Postgres (с паролем), → Redis, core-api/max → backend, → LLM (и что нужная модель есть), admin → core-api/stt/dsapi/annotator, dsapi → бот и Ollama |
| `test_04_artifacts` | embedding-модель, reranker, FAISS-индекс и что его размерность = размерности модели, промпты, модели dsapi/annotator, права на запись в тома |
| `test_05_database` | расширения postgis/vector/pg_trgm, все таблицы схемы, справочники, объём данных, известные объекты с ресурсами, висячие ссылки, `{{PUBLIC_BASE_URL}}` в путях к фото, геометрии |
| `test_06_redis` | Redis, ключи настроек админки, подписка max на `config:reload`, heartbeat бота |
| `test_07_services` | API каждого сервиса напрямую + контракты: всё, что вызывает core-api, есть в backend; токен админки принимает dsapi/annotator |
| `test_08_nginx` | маршруты docker-nginx (как их присылает cloud-b), webhook MAX, раздача фото из БД и карт, цепочка через nginx хоста и публичный адрес |
| `test_09_security` | без логина не открываются страницы админки, core-api и `/reload_database`; не пускает admin/admin; нет портов на 0.0.0.0 |
| `test_10_e2e` | запрос «Расскажи о байкальской нерпе» проходит core-api → LLM → backend → БД и через чат админки |

Маркеры: `slow`, `llm`, `public`, `host`, `security` (`-m "not public and not slow"` и т.п.).

## Настройки (переменные `HEALTH_*` или файл `tests/health/health.env`, он в .gitignore)

| Переменная | По умолчанию | |
|---|---|---|
| `HEALTH_ADMIN_PASSWORD` | `ADMIN_PASSWORD` из контейнера admin | если пароль меняли в UI |
| `HEALTH_SKIP_PUBLIC=1` / `HEALTH_SKIP_HOST=1` | — | не проверять публичный адрес / nginx хоста |
| `HEALTH_SERVICES` | `redis,db,backend,core-api,max,admin,nginx,dsapi,stt-service` | обязательные сервисы |
| `HEALTH_OPTIONAL_SERVICES` | `annotator` | проверяются, если запущены |
| `HEALTH_MIN_OBJECTS` / `HEALTH_MIN_RESOURCES` | `1000` / `1000` | порог «БД наполнена» |
| `HEALTH_KNOWN_OBJECTS` | `Байкальская нерпа,Байкальский омуль,Листвянка` | должны найтись и иметь ресурсы |
| `HEALTH_IGNORE_ENV` | — | переменные, которые не проверять (`SALUT_BOT_URL,DSAPI_URL`) |
| `HEALTH_LOG_IGNORE` | — | регулярка для известного шума в логах |
| `HEALTH_MAX_RESTARTS` | `0` | допустимое число перезапусков контейнера |
| `HEALTH_ALLOWED_PUBLIC_PORTS` | — | порты, которые можно публиковать на 0.0.0.0 (локально: `80,8080`) |

Локально (без cloud-b): `HEALTH_SKIP_PUBLIC=1 HEALTH_SKIP_HOST=1 HEALTH_ALLOWED_PUBLIC_PORTS=80,8080,8082,5432`.
