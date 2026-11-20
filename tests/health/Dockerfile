# Контейнер с тестами работоспособности. Запуск: bash scripts/healthcheck.sh
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /tests

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код тестов монтируется томом (правки без пересборки); COPY — на случай запуска без тома
COPY . .

ENTRYPOINT ["python", "-m", "pytest"]
