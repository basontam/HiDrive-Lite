FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN groupadd --system hidrive && useradd --system --gid hidrive --home-dir /app hidrive

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY --chown=hidrive:hidrive . .
RUN mkdir -p /app/data /app/secrets /app/data/strm \
    && chown -R hidrive:hidrive /app/data /app/secrets

USER hidrive
EXPOSE 12367

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import socket; s=socket.create_connection(('127.0.0.1',12367),2); s.close()"

CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:12367", "--error-logfile", "-", "app:app"]
