"""
Generador de viajes sinteticos (scripts/generar_datos_sinteticos.py).

No toca la base de datos: comprueba que lo generado tiene el formato y la
coherencia que la ingesta espera. Dentro del contenedor de la API no estan
scripts/ ni ingest/, asi que alli se omite.
"""

import csv
import importlib.util
import os
from collections import Counter
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
if not (RAIZ / "scripts" / "generar_datos_sinteticos.py").exists():
    pytest.skip("scripts/ no esta disponible aqui", allow_module_level=True)


def _cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


gen = _cargar("generar_datos_sinteticos", RAIZ / "scripts" / "generar_datos_sinteticos.py")
os.environ.setdefault("DATABASE_URL_ADMIN", "postgresql://no-se-usa")
ingesta = _cargar("ingesta", RAIZ / "ingest" / "ingesta.py")

with open(RAIZ / "ingest" / "datos" / "rows.csv", encoding="utf-8") as f:
    REALES = list(csv.DictReader(f))
PARTES = ["fare_amount", "tip_amount"]


def _residuo(v):
    """Lo que el total tiene ademas de tarifa y propina (recargos, tasas, peajes)."""
    return float(v["total_amount"]) - sum(float(v[c]) for c in PARTES)


@pytest.fixture(scope="module")
def viajes():
    return gen.generar(3000, semilla=7)


def test_misma_semilla_mismo_resultado():
    assert gen.generar(50, semilla=1) == gen.generar(50, semilla=1)
    assert gen.generar(50, semilla=1) != gen.generar(50, semilla=2)


def test_mismo_esquema_que_el_original(viajes):
    assert list(viajes[0].keys()) == list(REALES[0].keys())


def test_la_ingesta_lo_entiende_y_todo_es_de_2020(viajes):
    for v in viajes:
        salida = ingesta.parsear_fecha(v["tpep_pickup_datetime"])
        llegada = ingesta.parsear_fecha(v["tpep_dropoff_datetime"])
        assert salida is not None and llegada is not None
        anomalias = ingesta.detectar_anomalias(
            salida, llegada, ingesta.numero(v["total_amount"]), ingesta.numero(v["trip_distance"]))
        assert "fecha_fuera_de_periodo" not in anomalias


def test_el_total_cuadra_con_sus_partes(viajes):
    residuos_reales = {round(_residuo(v), 2) for v in REALES}
    for v in viajes:
        r = round(_residuo(v), 2)
        assert any(abs(r - real) <= 0.011 for real in residuos_reales), v


def test_conserva_el_reparto_entre_empresas(viajes):
    real = Counter(v["VendorID"] for v in REALES)
    sint = Counter(v["VendorID"] for v in viajes)
    for vendor, n in real.items():
        assert abs(sint[vendor] / len(viajes) - n / len(REALES)) < 0.05
