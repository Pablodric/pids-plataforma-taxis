FROM python:3.12-slim
WORKDIR /ingest
RUN pip install --no-cache-dir "psycopg[binary]>=3.2"
COPY ingesta.py .
COPY datos/ ./datos/
CMD ["python", "ingesta.py"]
