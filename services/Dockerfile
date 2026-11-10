FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROMETHEUS_DISABLE_CREATED_SERIES=True
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY svc ./svc
RUN useradd --create-home --uid 10001 app
USER app
# The command (which service to run) is set per container in docker-compose.yml.
