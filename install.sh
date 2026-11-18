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

echo "=== [3/6] Создание рабочих директорий и настройка прав ==="
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

echo "=== [5/6] Запуск PostgreSQL (db) и Redis ==="
$DOCKER_CMD compose -f docker-compose.yml up -d db redis

echo "Ожидание готовности PostgreSQL..."
until $DOCKER_CMD compose -f docker-compose.yml exec -T db pg_isready -U "${DB_USER:-postgres}" -d "${DB_NAME:-eco}" >/dev/null 2>&1; do
    sleep 2
done

echo "=== [6/6] Запуск всех сервисов EcoAssistant ==="
$DOCKER_CMD compose -f docker-compose.yml up -d

echo "Ожидание готовности контейнера backend..."
RETRIES=30
until $DOCKER_CMD compose -f docker-compose.yml exec -T backend python -c "import sys; sys.exit(0)" 2>/dev/null; do
    RETRIES=$((RETRIES - 1))
    [ "$RETRIES" -le 0 ] && break
    sleep 2
done

echo "=================================================================="
echo "Развёртывание успешно завершено!"
echo "Внутренний адрес (по IP): http://172.25.4.35/ecoassistant/admin/"
echo "Внешний адрес (ИРНИТУ):   https://cloud-b.istu.edu/ecoassistant/admin/"
echo ""
echo "Если нужно скачать ML-модели (около 7 ГБ): bash scripts/download_models.sh"
echo "Если нужно первично заполнить БД:          bash scripts/init_db.sh"
echo "=================================================================="
