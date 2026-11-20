#!/usr/bin/env bash
# Тесты работоспособности всего стека (tests/health). Код возврата = код pytest.
#
#   bash scripts/healthcheck.sh                  # всё
#   bash scripts/healthcheck.sh -m "not slow"    # быстро: без LLM и распознавания речи
#   bash scripts/healthcheck.sh -k nginx         # только то, что про nginx
#   HEALTH_SKIP_PUBLIC=1 bash scripts/healthcheck.sh
#
# Отчёт JUnit: logs/health-report.xml
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
fi

echo "[health] проверка docker compose config"
$DOCKER compose config -q

echo "[health] сборка образа тестов"
$DOCKER compose --profile health build -q health-tests

# Пробрасываем все HEALTH_* из окружения (со значением — sudo окружение не передаёт)
env_args=()
while IFS='=' read -r name value; do
    env_args+=(-e "$name=$value")
done < <(env | grep '^HEALTH_' || true)

tty_args=()
[ -t 1 ] || tty_args=(-T)

exec $DOCKER compose --profile health run --rm \
    ${tty_args[@]+"${tty_args[@]}"} ${env_args[@]+"${env_args[@]}"} \
    health-tests --junitxml=/reports/health-report.xml "$@"
