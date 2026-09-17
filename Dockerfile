FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OPENSHELF_DATA_DIR=/data OPENSHELF_DOWNLOAD_DIR=/downloads
WORKDIR /app
COPY pyproject.toml README.md LICENSE requirements.lock ./
COPY openshelf ./openshelf
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && pip install --no-cache-dir --no-deps . && useradd --uid 1000 --create-home shelf
RUN mkdir /data /downloads && chown shelf:shelf /data /downloads
USER shelf
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"
CMD ["openshelf", "--host", "0.0.0.0", "--port", "8080"]
