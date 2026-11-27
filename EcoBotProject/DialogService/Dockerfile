FROM python:3.10-slim

WORKDIR /app

# Устанавливаем общие зависимости
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

# Копируем весь код подмодуля
COPY . .

# Заводские промпты в отдельный каталог: /app/prompts перекрывается volume,
# поэтому дефолты храним здесь и при первом старте копируем в постоянный каталог.
COPY prompts ./prompts_defaults

# Мы НЕ пишем здесь CMD, так как у нас две разные точки входа