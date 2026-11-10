FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY actuator.py .
# Runs as root only because the Docker socket requires it; it is reachable solely
# from VIGIL's engine on the private control network, and only with a token.
CMD ["uvicorn", "actuator:app", "--host", "0.0.0.0", "--port", "8000"]
