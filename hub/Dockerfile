# Build from the repo root: docker build -f hub/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY hub/requirements.txt hub/requirements.txt
RUN pip install --no-cache-dir -r hub/requirements.txt
COPY hub ./hub
COPY app/static ./static
COPY install.sh .
ENV HUB_DATA_DIR=/data STATIC_DIR=/app/static INSTALL_SH=/app/install.sh PYTHONUNBUFFERED=1
EXPOSE 9000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9000/healthz')"
CMD ["uvicorn", "hub.main:app", "--host", "0.0.0.0", "--port", "9000", "--proxy-headers", "--forwarded-allow-ips", "*"]
