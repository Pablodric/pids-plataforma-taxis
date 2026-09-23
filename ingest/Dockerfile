FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /ingest
RUN pip install --no-cache-dir "psycopg[binary]>=3.2,<4"
COPY ingesta.py .
COPY datos/ ./datos/
CMD ["python", "ingesta.py"]
