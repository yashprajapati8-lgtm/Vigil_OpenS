FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vigil ./vigil
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app /data
USER app
EXPOSE 8080
CMD ["uvicorn", "vigil.api:app", "--host", "0.0.0.0", "--port", "8080"]
