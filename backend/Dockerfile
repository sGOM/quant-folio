FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 빌드 의존성 (asyncpg, cryptography, vectorbt 등)
# postgresql-client: worker 의 야간 DB 백업(E-2)이 pg_dump 로 db 서비스에 직접 접속한다.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8000

# 기본 커맨드는 web. engine/worker 는 compose 에서 override 한다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
