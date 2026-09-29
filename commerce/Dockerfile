FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKLOAD_PATH=/data/workload.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8200

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/healthz', timeout=2)"]

# Single process: orders, refunds and idempotency keys live in memory.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8200"]
