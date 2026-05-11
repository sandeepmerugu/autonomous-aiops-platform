FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn prometheus-client python-dotenv
COPY app.py .
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/health/live')"
EXPOSE 8001
CMD ["python", "app.py"]
