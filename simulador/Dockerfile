FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /simulador
# Sin dependencias: solo la biblioteca estandar de Python.
COPY simulador/simulador.py .
COPY ingest/datos/rows.csv .
CMD ["python", "simulador.py"]
