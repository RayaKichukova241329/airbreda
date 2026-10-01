FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends redis-tools \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir requests pandas psycopg2-binary python-dotenv redis
COPY common.py ingest_air.py ./
CMD ["python", "ingest_air.py"]