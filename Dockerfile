FROM python:3.12-slim

WORKDIR /app

COPY requirements-backend.txt .
RUN pip install --no-cache-dir -r requirements-backend.txt

COPY . .

RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser /app
USER appuser

CMD ["python3", "cc2_connector.py"]
