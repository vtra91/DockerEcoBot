#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "=== [Bootstrap] Настройка ALT Server 11.1.1 (p11) и окружения проекта ==="

# 1. Удаление мусорных файлов нулевой длины из корня проекта
rm -f "26.1.2" "=" "=0.15.1" "=0.20" "=0.8.0" "=1.0.0" "=13.0" "=2.4.0" "=5.2.1" \
      "CACHED" "ERROR" "exporting" "Get-ChildItem" "naming" "resolve" "transferring" "unpacking" "[internal]" || true

# 2. Загрузка обязательных модулей ядра для Docker
for mod in overlay br_netfilter nf_tables; do
    if ! lsmod | grep -q "^${mod}"; then
        echo "[Bootstrap] Загрузка модуля ядра ${mod}..."
        sudo modprobe "${mod}" || true
    fi
done

if [ ! -f /etc/modules-load.d/docker.conf ]; then
    printf "overlay\nbr_netfilter\nnf_tables\n" | sudo tee /etc/modules-load.d/docker.conf >/dev/null
fi

# 3. Настройка сетевых параметров sysctl
if [ ! -f /etc/sysctl.d/99-docker.conf ]; then
    cat << 'EOF' | sudo tee /etc/sysctl.d/99-docker.conf >/dev/null
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
    sudo sysctl --system >/dev/null 2>&1 || true
fi

# 4. Установка Docker и Docker Compose v2 на ALT Linux (p11)
if ! command -v docker >/dev/null 2>&1; then
    echo "[Bootstrap] Установка docker-engine и docker-compose-v2..."
    if [ -f /etc/altlinux-release ]; then
        sudo apt-get update
        sudo apt-get install -y docker-engine docker-compose-v2 docker-buildx curl git jq python3
    else
        curl -fsSL https://get.docker.com | sudo sh
    fi
fi

sudo systemctl enable --now docker
sudo usermod -aG docker "${SUDO_USER:-${USER:-assistant}}" 2>/dev/null || true

# 5. Расширение Swap до 8 ГБ на Btrfs, чтобы ML-модели не падали по OOM
TOTAL_SWAP_MB=$(free -m | awk '/^Swap:/ {print $2}')
if [ "${TOTAL_SWAP_MB:-0}" -lt 4000 ] && [ ! -f /swapfile ]; then
    echo "[Bootstrap] Создаём дополнительный /swapfile на 8 ГБ..."
    if df -T / | grep -q btrfs; then
        sudo btrfs filesystem mkswapfile --size 8g /swapfile || true
    else
        sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile || true
    fi
    if [ -f /swapfile ]; then
        sudo swapon /swapfile || true
        grep -q "/swapfile" /etc/fstab || echo "/swapfile none swap defaults 0 0" | sudo tee -a /etc/fstab >/dev/null
    fi
fi

# 6. Освобождение порта 80 (остановка системного nginx на хосте)
if systemctl is-active --quiet nginx 2>/dev/null; then
    echo "[Bootstrap] Остановка системного nginx на хосте для освобождения порта 80..."
    sudo systemctl disable --now nginx || true
fi

# 7. Генерация shared.env и всех .env файлов микросервисов из .env.example
if [ ! -f shared.env ] && [ -f shared.env.example ]; then
    cp shared.env.example shared.env
    echo "[Bootstrap] Создан shared.env"
fi

# Гарантируем правильные адреса для cloud-b.istu.edu/ecoassistant в shared.env
grep -q "^BASE_PATH=" shared.env || echo "BASE_PATH=/ecoassistant" >> shared.env
sed -i 's|^BASE_PATH=.*|BASE_PATH=/ecoassistant|' shared.env
sed -i 's|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=https://cloud-b.istu.edu/ecoassistant|' shared.env
sed -i 's|^BASE_URL_MAPS=.*|BASE_URL_MAPS=https://cloud-b.istu.edu/ecoassistant/maps/|' shared.env
sed -i 's|^ECOBOT_API_BASE_URL=.*|ECOBOT_API_BASE_URL=http://backend:8000|' shared.env

for example in \
    "db_custom/.env.example" \
    "salut_bot/.env.example" \
    "EcoBotProject/DialogService/.env.example" \
    "dsapi/.env.example" \
    "images-extractor/.env.example" \
    "stt_service/.env.example" \
    "annotator/.env.example" \
    "AdminPanel/.env.example"; do
    if [ -f "$example" ]; then
        target="${example%.example}"
        if [ ! -f "$target" ]; then
            cp "$example" "$target"
            echo "[Bootstrap] Создан $target"
        fi
    fi
done

# Синхронизируем пароль БД между shared.env и db_custom/.env
DB_PASS=$(grep '^DB_PASSWORD=' shared.env | cut -d= -f2- || echo "changeme")
if [ -f db_custom/.env ]; then
    sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${DB_PASS}|" db_custom/.env
fi

# Генерируем SESSION_SECRET_KEY в AdminPanel/.env, если там заглушка
if [ -f AdminPanel/.env ] && grep -q "replace-with-random-secret" AdminPanel/.env; then
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    sed -i "s|replace-with-random-secret|${SECRET}|" AdminPanel/.env
fi

cp -f nginx/nginx.conf nginx/nginx.http.conf 2>/dev/null || true

echo "=== [Bootstrap] Готово! ==="
