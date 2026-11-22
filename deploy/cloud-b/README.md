# Деплой на EcoAssistant (https://cloud-b.istu.edu/ecoassistant/)

```
браузер ─https─▶ cloud-b: location /ecoassistant/ { proxy_pass http://172.25.4.35/; }   (срезает префикс)
        ─http──▶ системный nginx :80   (/etc/nginx/sites-enabled.d/ecoassistant.conf)
        ───────▶ docker-nginx 127.0.0.1:8088   (nginx/nginx.http.conf)
        ───────▶ backend / admin / core-api / max
```

Префикс срезается **один раз** — на cloud-b. Дальше всё работает от корня.
Сервисы знают префикс только из `shared.env` и подставляют его в то, что уходит в браузер:

| Переменная (shared.env) | Значение на этом сервере | Где используется |
|---|---|---|
| `BASE_PATH` | `/ecoassistant` | редиректы и ссылки админки, cookie сессии, `/docs` backend и core-api |
| `PUBLIC_BASE_URL` | `https://cloud-b.istu.edu/ecoassistant` | ссылки на карты и фото, webhook MAX (`+ /max-webhook`) |
| `BASE_URL_MAPS` | `https://cloud-b.istu.edu/ecoassistant/maps/` | ссылки на карты |

Публичные адреса: админка `…/ecoassistant/admin/`, API `…/ecoassistant/search/…`,
core-api `…/ecoassistant/core-api/docs`, webhook `…/ecoassistant/max-webhook`.

## Первый запуск

```bash
# 0. Docker (один раз; после usermod перелогиниться)
sudo systemctl enable --now docker
sudo usermod -aG docker assistant

cd /srv/DockerEcoBot
git pull

# 1. Конфиги
cp shared.env.example shared.env
cp db_custom/.env.example db_custom/.env
cp salut_bot/.env.example salut_bot/.env
cp AdminPanel/.env.example AdminPanel/.env
cp EcoBotProject/DialogService/.env.example EcoBotProject/DialogService/.env
cp dsapi/.env.example dsapi/.env
touch images-extractor/.env
echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.cloudb.yml' > .env
```

В `shared.env` задать `BASE_PATH`, `PUBLIC_BASE_URL`, `BASE_URL_MAPS` (см. таблицу), `DB_PASSWORD`,
`MAX_BOT_TOKEN`, `BOT_TOKEN`, LLM_*. Тот же пароль БД — в `db_custom/.env` (`POSTGRES_PASSWORD`)
и в `AdminPanel/.env` (`DB_PASSWORD`). В `AdminPanel/.env` — `SESSION_SECRET_KEY`
(`python3 -c "import secrets; print(secrets.token_hex(32))"`) и `ADMIN_PASSWORD` пароль первого входа.

```bash
# 2. Каталоги, модели, запуск
mkdir -p data/postgres_data data/admin_db logs maps salut_bot/images salut_bot/embedding_models \
         salut_bot/knowledge_base_scripts/Vector/faiss_index dsapi/local_models dsapi/logs dsapi/results
bash scripts/download_models.sh
docker compose up -d --build

# 3. Наполнение БД (фото берём с боевого сервера, пока salut_bot/images пустая)
IMPORT_IMAGES_BASE_URL=https://testecobot.ru bash scripts/init_db.sh

# 4. Системный nginx
sudo cp deploy/cloud-b/ecoassistant.conf /etc/nginx/sites-enabled.d/ecoassistant.conf
sudo nginx -t && sudo systemctl reload nginx
```

## Проверка

```bash
curl -sI http://127.0.0.1/admin/ | grep -i location   # → location: /ecoassistant/admin/login
curl -s  http://127.0.0.1/health                       # → {"status":"ok"}
```
Затем в браузере: https://cloud-b.istu.edu/ecoassistant/admin/

После правки `shared.env` нужен `docker compose up -d` (пересоздание контейнеров),
а не `restart`: `restart` не перечитывает env_file.
