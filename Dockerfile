FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    gcc g++ make libssl-dev curl \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

CMD ["python", "bot.py"]
