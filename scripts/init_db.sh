#!/usr/bin/env bash
set -euo pipefail

MODE="${1:---full}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

DOCKER_CMD="docker"
if ! docker info >/dev/null 2>&1; then
    DOCKER_CMD="sudo docker"
fi

set -a
[ -f shared.env ] && source shared.env 2>/dev/null || true
set +a

PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://cloud-b.istu.edu/ecoassistant}"

echo "[init_db] Режим: $MODE"
echo "[init_db] PUBLIC_BASE_URL: $PUBLIC_BASE_URL"

$DOCKER_CMD compose exec -T backend bash -c "
    sed 's|{{PUBLIC_BASE_URL}}|${PUBLIC_BASE_URL}|g' \
        /app/json_files/resources.json \
        > /app/json_files/resources_deploy.json

    cd knowledge_base_scripts/Relational
    python -m db_importer.main ${MODE} \
        --resources-file /app/json_files/resources_deploy.json

    rm -f /app/json_files/resources_deploy.json
"

echo "[init_db] Готово!"
