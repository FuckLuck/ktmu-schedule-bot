FROM python:3.12-slim

WORKDIR /app

# Установка системных утилит и часового пояса
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Moscow
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Установка зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода приложения
COPY . .

# Точка входа
CMD ["python", "main.py"]
