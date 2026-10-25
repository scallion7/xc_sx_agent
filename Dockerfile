FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_AUTO_CREATE=false

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-lock.txt ./
RUN python -m pip install --no-cache-dir --upgrade \
        "setuptools>=84.0.0" \
        "wheel>=0.46.2" \
        "jaraco.context>=6.1.0" \
    && python -m pip install --no-cache-dir -r requirements-lock.txt

COPY . .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/app/sessions /inventory \
    && chown -R appuser:appuser /app /inventory

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --retries=5 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready')"

CMD ["sh", "-c", "python -m app.scripts.manage_migrations upgrade && exec python -m uvicorn app.api:app --host 0.0.0.0 --port 8000"]
