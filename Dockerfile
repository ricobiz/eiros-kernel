FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libnss3 libxss1 libasound2 libatk-bridge2.0-0 libgtk-3-0 \
    libgbm1 libxshmfence1 \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
