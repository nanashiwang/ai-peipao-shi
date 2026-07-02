FROM python:3.11-slim

WORKDIR /app
# ca-certificates：会话内容存档 SDK 通过 HTTPS 拉取腾讯侧数据需要根证书。
# 镜像自带 libssl3；若拿到真实 SDK(.so) 后 `ldd` 报缺 libcrypto.so.1.1，
# 需另行补装 OpenSSL 1.1 运行库（详见 docs/会话内容存档接入.md）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.server.txt .
RUN pip install --no-cache-dir -r requirements.server.txt
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
COPY app ./app
COPY deploy ./deploy
COPY rpa ./rpa
COPY samples ./samples
COPY config/ark.example.json ./config/ark.example.json
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
