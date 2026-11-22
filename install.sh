#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Конвертация возможных CRLF окончаний строк в LF
find . -maxdepth 3 -type f \( -name "*.sh" -o -name "*.py" -o -name "*.env*" \) -exec sed -i 's/\r$//' {} + 2>/dev/null || true

echo "=== [1/6] Запуск bootstrap.sh ==="
bash ./bootstrap.sh

echo "=== [2/6] Патчинг всех микросервисов и AdminPanel под BASE_PATH из shared.env ==="
python3 ./patch_base_path.py

DOCKER_CMD="docker"
if ! docker info >/dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
fi

set -a
[ -f shared.env ] && source shared.env
set +a

ask "MAX Bot Token (Enter = оставить из shared.env):"
read -r -s -p "MAX_BOT_TOKEN: " MAX_BOT_TOKEN_INPUT < /dev/tty
echo ""

ask "GigaChat Credentials (Enter = оставить из shared.env):"
read -r -s -p "GIGACHAT_CREDENTIALS: " GIGACHAT_CREDENTIALS_INPUT < /dev/tty
echo ""

CURRENT_GIGACHAT_MODEL=""
if [[ -f "$REPO_ROOT/shared.env" ]]; then
    CURRENT_GIGACHAT_MODEL="$(grep -E '^GIGACHAT_MODEL=' "$REPO_ROOT/shared.env" | head -n1 | cut -d= -f2- || true)"
fi
DEFAULT_GIGACHAT_MODEL="${CURRENT_GIGACHAT_MODEL:-GigaChat-2-Max}"

ask "Модель GigaChat (Enter = $DEFAULT_GIGACHAT_MODEL):"
read -r -p "GIGACHAT_MODEL: " GIGACHAT_MODEL_INPUT < /dev/tty
GIGACHAT_MODEL_INPUT="${GIGACHAT_MODEL_INPUT:-$DEFAULT_GIGACHAT_MODEL}"

ask "Sber Key Enterprise (Enter = оставить из shared.env):"
read -r -s -p "SBER_KEY_ENTERPRICE: " SBER_KEY_ENTERPRICE_INPUT < /dev/tty
echo ""
echo ""

# -----------------------------------------------------------
# 3. Генерация shared.env
# -----------------------------------------------------------
log "Настройка shared.env..."

if [[ ! -f "$REPO_ROOT/shared.env" ]]; then
    cp "$REPO_ROOT/shared.env.example" "$REPO_ROOT/shared.env"
    warn "shared.env создан из шаблона — заполните оставшиеся секреты вручную"
fi

sed -i "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$PUBLIC_BASE_URL|" "$REPO_ROOT/shared.env"
sed -i "s|^BASE_URL_MAPS=.*|BASE_URL_MAPS=$PUBLIC_BASE_URL/maps/|" "$REPO_ROOT/shared.env"

if [[ -n "$DB_PASSWORD_INPUT" ]]; then
    sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$DB_PASSWORD_INPUT|" "$REPO_ROOT/shared.env"
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$DB_PASSWORD_INPUT|" "$REPO_ROOT/db_custom/.env" 2>/dev/null || true
fi

if [[ -n "$MAX_BOT_TOKEN_INPUT" ]]; then
    sed -i "s|^MAX_BOT_TOKEN=.*|MAX_BOT_TOKEN=$MAX_BOT_TOKEN_INPUT|" "$REPO_ROOT/shared.env"
fi

if [[ -n "$GIGACHAT_CREDENTIALS_INPUT" ]]; then
    sed -i "s|^GIGACHAT_CREDENTIALS=.*|GIGACHAT_CREDENTIALS=$GIGACHAT_CREDENTIALS_INPUT|" "$REPO_ROOT/shared.env"
fi

if grep -q "^GIGACHAT_MODEL=" "$REPO_ROOT/shared.env"; then
    sed -i "s|^GIGACHAT_MODEL=.*|GIGACHAT_MODEL=$GIGACHAT_MODEL_INPUT|" "$REPO_ROOT/shared.env"
else
    printf 'GIGACHAT_MODEL=%s\n' "$GIGACHAT_MODEL_INPUT" >> "$REPO_ROOT/shared.env"
fi

if [[ -n "$SBER_KEY_ENTERPRICE_INPUT" ]]; then
    sed -i "s|^SBER_KEY_ENTERPRICE=.*|SBER_KEY_ENTERPRICE=$SBER_KEY_ENTERPRICE_INPUT|" "$REPO_ROOT/shared.env"
fi

log "shared.env готов"

# -----------------------------------------------------------
# 3b. Генерация db_custom/.env
# -----------------------------------------------------------
if [[ ! -f "$REPO_ROOT/db_custom/.env" ]]; then
    cp "$REPO_ROOT/db_custom/.env.example" "$REPO_ROOT/db_custom/.env"
fi
DB_PASS="${DB_PASSWORD_INPUT:-$(grep '^DB_PASSWORD=' "$REPO_ROOT/shared.env" | cut -d= -f2-)}"
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$DB_PASS|" "$REPO_ROOT/db_custom/.env"
log "db_custom/.env готов"

for example in \
    "$REPO_ROOT/salut_bot/.env.example" \
    "$REPO_ROOT/EcoBotProject/DialogService/.env.example" \
    "$REPO_ROOT/dsapi/.env.example" \
    "$REPO_ROOT/images-extractor/.env.example"; do
    target="${example%.example}"
    if [[ ! -f "$target" ]]; then
        cp "$example" "$target"
        log "Создан $(basename $(dirname $target))/$(basename $target) из шаблона"
    fi
done

# -----------------------------------------------------------
# 4. AdminPanel — генерация SESSION_SECRET_KEY
# -----------------------------------------------------------
if [[ ! -f "$REPO_ROOT/AdminPanel/.env" ]]; then
    cp "$REPO_ROOT/AdminPanel/.env.example" "$REPO_ROOT/AdminPanel/.env"
fi

if grep -q "replace-with-random-secret" "$REPO_ROOT/AdminPanel/.env"; then
    SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|replace-with-random-secret|$SESSION_SECRET|" "$REPO_ROOT/AdminPanel/.env"
    log "SESSION_SECRET_KEY сгенерирован"
fi

# -----------------------------------------------------------
# 5. Nginx — выбор конфига
# -----------------------------------------------------------
log "Настройка nginx..."
mkdir -p "$REPO_ROOT/certbot/www" "$REPO_ROOT/certbot/conf"

if [[ "$USE_SSL" == true ]]; then
    sed "s/\${DOMAIN}/$DOMAIN/g" "$REPO_ROOT/nginx/nginx.https.conf" > "$REPO_ROOT/nginx/nginx.conf"
    log "nginx: HTTPS режим (домен: $DOMAIN)"
    warn "Для получения сертификата Let's Encrypt выполните ПОСЛЕ первого запуска:"
    warn "  docker run --rm -v \$(pwd)/certbot/www:/var/www/certbot -v \$(pwd)/certbot/conf:/etc/letsencrypt certbot/certbot certonly --webroot -w /var/www/certbot -d $DOMAIN --email your@email.com --agree-tos"
else
    cp "$REPO_ROOT/nginx/nginx.http.conf" "$REPO_ROOT/nginx/nginx.conf"
    log "nginx: HTTP-only режим"
fi

# -----------------------------------------------------------
# 6. Создание необходимых директорий
# -----------------------------------------------------------
log "Создание директорий..."
mkdir -p \
    data/postgres_data \
    data/admin_db \
    logs \
    maps \
    certbot/www \
    certbot/conf \
    salut_bot/images \
    salut_bot/embedding_models \
    salut_bot/knowledge_base_scripts/Vector/faiss_index \
    dsapi/local_models \
    dsapi/logs \
    dsapi/results

chmod -R 777 data/admin_db logs maps dsapi/local_models dsapi/logs dsapi/results salut_bot/embedding_models 2>/dev/null || \
    sudo chmod -R 777 data/admin_db logs maps dsapi/local_models dsapi/logs dsapi/results salut_bot/embedding_models

echo "=== [4/6] Сборка Docker-образов ==="
$DOCKER_CMD compose -f docker-compose.yml build
# -----------------------------------------------------------
# 9. Запуск PostgreSQL и Redis, ожидание готовности
# -----------------------------------------------------------
echo "=== [5/6] Запуск PostgreSQL (db) и Redis ==="
$DOCKER_CMD compose up -d db redis

echo "Ожидание готовности PostgreSQL..."
RETRIES=30
until $DOCKER_CMD compose exec -T db pg_isready -U "${DB_USER:-postgres}" -d "${DB_NAME:-eco}" >/dev/null 2>&1; do
    RETRIES=$((RETRIES - 1))
    [[ $RETRIES -le 0 ]] && die "PostgreSQL не запустился за 60 секунд"
    sleep 2
done
log "PostgreSQL готов"

# -----------------------------------------------------------
# 9b. Инициализация таблиц Админ-панели (ИЗ HEAD)
# -----------------------------------------------------------
log "Создание таблиц для Админ-панели..."
$DOCKER_CMD compose exec -T admin python -c "
from sqlalchemy import create_engine
from models.models import Base
import os
db_url = f\"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME')}\"
engine = create_engine(db_url)
Base.metadata.create_all(bind=engine)
" || warn "Не удалось автоматически создать таблицы для Админ-панели. Возможно, они уже существуют."
log "Таблицы Админ-панели готовы"

# -----------------------------------------------------------
# 9c. Включение pg_trgm для поиска (ИЗ HEAD)
# -----------------------------------------------------------
log "Включение расширения pg_trgm в PostgreSQL..."
$DOCKER_CMD compose exec -T db psql -U postgres -d eco -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;" || warn "Не удалось включить pg_trgm."
log "Расширение pg_trgm готово"

# -----------------------------------------------------------
# 10. Запуск остальных сервисов и ожидание backend
# -----------------------------------------------------------
echo "=== [6/6] Запуск всех сервисов EcoAssistant ==="
$DOCKER_CMD compose up -d

echo "Ожидание готовности контейнера backend..."
RETRIES=30
until $DOCKER_CMD compose exec -T backend python -c "import sys; sys.exit(0)" 2>/dev/null; do
    RETRIES=$((RETRIES - 1))
    [[ $RETRIES -le 0 ]] && die "backend не запустился за 60 секунд"
    sleep 2
done
log "backend готов"

echo "=================================================================="
echo "Развёртывание успешно завершено!"
echo "Внутренний адрес (по IP): http://172.25.4.35/ecoassistant/admin/"
echo "Внешний адрес (ИРНИТУ):   https://cloud-b.istu.edu/ecoassistant/admin/"
echo ""
echo "Если нужно скачать ML-модели (около 7 ГБ): bash scripts/download_models.sh"
echo "Если нужно первично заполнить БД:          bash scripts/init_db.sh"
echo "=================================================================="
