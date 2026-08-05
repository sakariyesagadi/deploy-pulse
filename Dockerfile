FROM python:3.11-slim

# Keep Python lean and unbuffered inside the container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application.
COPY src/ ./src/
COPY templates/ ./templates/
COPY config.yml ./config.yml

# Persisted SQLite DB and generated dashboard live under /data by default.
VOLUME ["/data"]
ENV DEPLOY_PULSE_DB=/data/deploy_pulse.db

# Default: collect runs, render the dashboard, then send the digest.
CMD ["sh", "-c", "python -m src.collector && python -m src.dashboard && python -m src.notify"]
