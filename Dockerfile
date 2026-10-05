FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала зависимости — этот слой кешируется и не пересобирается при правке кода
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt

COPY src ./src
COPY sql ./sql
COPY config ./config
COPY tests ./tests

CMD ["python", "-m", "weather", "check"]
