FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY requirements-backend.txt .
RUN pip install --no-cache-dir -r requirements-backend.txt

COPY . .

RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /app/certs \
    && chown -R appuser /app \
    && chmod +x /app/docker-entrypoint.sh
USER appuser

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python3", "cc2_connector.py"]
