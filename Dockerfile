FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install font packages for proper Chinese rendering in headless browser
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

COPY publisher ./publisher
COPY alembic ./alembic
COPY alembic.ini ./

RUN mkdir -p /app/data /app/data/profile /app/data/media /app/data/artifacts

EXPOSE 8790

CMD ["uvicorn", "publisher.main:app", "--host", "0.0.0.0", "--port", "8790"]
