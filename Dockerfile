# Image légère adaptée au Raspberry Pi 3 (arm64/armv7).
FROM python:3.12-slim

WORKDIR /app

# Dépendances système minimales pour web3/httpx (compilation éventuelle de cytoolz/ckzg).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --extra-index-url https://www.piwheels.org/simple -r requirements.txt

COPY . .

RUN mkdir -p data logs

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py", "run"]
