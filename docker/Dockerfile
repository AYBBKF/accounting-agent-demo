FROM python:3.12-slim

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# Outils OCR systeme : tesseract (moteur, francais + anglais) et poppler
# (rasterisation des PDF scannes). Indispensables pour lire une facture
# photographiee ; sans eux, toute image partirait en quarantaine.
RUN apt-get update && apt-get install --no-install-recommends -y \
        tesseract-ocr \
        tesseract-ocr-fra \
        tesseract-ocr-eng \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app

RUN mkdir -p /app/data/exports && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=15s \
  CMD find /app/data/heartbeat -mmin -2 | grep -q heartbeat || exit 1

CMD ["python", "-m", "app.bot"]
