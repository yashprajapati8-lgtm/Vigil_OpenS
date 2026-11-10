FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY chaos_controller.py .
CMD ["uvicorn", "chaos_controller:app", "--host", "0.0.0.0", "--port", "8090"]
