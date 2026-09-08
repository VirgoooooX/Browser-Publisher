FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install Chinese fonts, curl, and certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY publisher ./publisher
COPY alembic ./alembic
COPY alembic.ini ./

# Install application dependencies and ONLY Chromium with its required system dependencies
RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/data /app/data/profile /app/data/media /app/data/artifacts

EXPOSE 8790

CMD ["uvicorn", "publisher.main:app", "--host", "0.0.0.0", "--port", "8790"]
