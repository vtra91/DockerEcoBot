FROM python:3.10-slim

WORKDIR /app

# Копируем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код
COPY . .

# Заводской FAQ отдельно: /app/faq перекрывается volume,
# поэтому дефолт храним в /app/faq_defaults и засеваем при первом старте.
COPY faq ./faq_defaults

# Запускаем FastAPI
CMD ["uvicorn", "main_fastapi:app", "--host", "0.0.0.0", "--port", "8000"]