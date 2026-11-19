# Образ для Fly.io (собирается на их сервере командой fly deploy)
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot ./bot
COPY webapp ./webapp

# data/ (база, логи, копии) — постоянный диск Fly, подключается в /app/data (см. fly.toml)
EXPOSE 8080
CMD ["python", "-m", "bot.supervisor"]
