FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY mockllm ./mockllm

RUN useradd --create-home --uid 10001 provider
USER provider

EXPOSE 8100

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=2)"

# One worker on purpose: rate limits and the ledger are held in this process's memory.
CMD ["uvicorn", "mockllm.app:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "1"]
