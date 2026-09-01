FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# SQLite lives on a mounted volume so alerts survive rebuilds
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8080
# Default is the web UI; docker-compose overrides this for the engine service.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "60", "src.web:app"]
