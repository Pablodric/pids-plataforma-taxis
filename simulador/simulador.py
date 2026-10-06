#!/usr/bin/env python3
"""Simulador de viajes en tiempo real.

Genera un viaje sintético cada pocos segundos, a partir de filas reales de
rows.csv, y lo envía a la API de la plataforma (POST /ingesta/viajes) con la
credencial de la empresa a la que pertenece. Nunca escribe en la base de
datos: como cualquier proveedor, solo habla con la API.

    python simulador.py                    # API en http://localhost:8000
    API_URL=http://api:8000 python simulador.py
    python simulador.py --solo-mostrar     # no envía, solo imprime

Variables de entorno: API_URL, INTERVALO_SEG, CLAVE_PROVEEDOR, RUTA_CSV.
"""

from __future__ import annotations

import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

API_URL = os.environ.get("API_URL", "http://localhost:8000").rstrip("/")
INTERVALO_SEG = float(os.environ.get("INTERVALO_SEG", "3"))
CLAVE = os.environ.get("CLAVE_PROVEEDOR", "demo1234")
RUTA_CSV = Path(os.environ.get("RUTA_CSV", Path(__file__).with_name("rows.csv")))

# Cuenta de proveedor de cada empresa, según el VendorID del viaje. La
# empresa la decide la API a partir de esta credencial, no del dato.
CUENTAS = {
    "1": "sistema@taxisnorte.es",
    "2": "sistema@movilidadsur.es",
}

DATE_FORMAT = "%m/%d/%Y %I:%M:%S %p"
EXPECTED_COLUMNS = [
    "VendorID",
    "tpep_pickup_datetime",
    "tpep_dropoff_datetime",
    "passenger_count",
    "trip_distance",
    "RatecodeID",
    "store_and_fwd_flag",
    "PULocationID",
    "DOLocationID",
    "payment_type",
    "fare_amount",
    "extra",
    "mta_tax",
    "tip_amount",
    "tolls_amount",
    "improvement_surcharge",
    "total_amount",
    "congestion_surcharge",
]


def money(value: float) -> str:
    return f"{max(0.0, value):.2f}"


def load_reference(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"no existe el CSV de referencia: {path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPECTED_COLUMNS:
            raise ValueError(
                "el CSV de referencia no tiene el esquema esperado. "
                f"Columnas encontradas: {reader.fieldnames}"
            )
        rows = list(reader)

    if not rows:
        raise ValueError("el CSV de referencia no contiene datos")
    return rows


def make_row(
    template: dict[str, str],
    rng: random.Random,
    end_date: date,
    days_back: int,
) -> dict[str, str]:
    template_pickup = datetime.strptime(
        template["tpep_pickup_datetime"], DATE_FORMAT
    )
    generated_day = end_date - timedelta(days=rng.randint(0, days_back))
    pickup = datetime.combine(generated_day, template_pickup.time())

    duration = rng.randint(2 * 60, 75 * 60)
    distance = max(0.1, float(template["trip_distance"]) * rng.uniform(0.75, 1.25))
    duration = max(60, int(duration * max(0.6, min(2.0, distance / 2.0))))
    dropoff = pickup + timedelta(seconds=duration)

    passengers = max(1, min(6, int(round(float(template["passenger_count"])))))
    if rng.random() < 0.08:
        passengers = 1

    fare = max(2.5, float(template["fare_amount"]) * rng.uniform(0.85, 1.20))
    extra = max(0.0, float(template["extra"]) * rng.uniform(0.8, 1.2))
    mta_tax = 0.5
    tip = max(0.0, float(template["tip_amount"]) * rng.uniform(0.7, 1.3))
    tolls = max(0.0, float(template["tolls_amount"]) * rng.uniform(0.8, 1.2))
    improvement = 0.3
    congestion = max(
        0.0, float(template["congestion_surcharge"]) * rng.uniform(0.8, 1.2)
    )
    total = fare + extra + mta_tax + tip + tolls + improvement + congestion

    pickup_zone = rng.randint(1, 265)
    dropoff_zone = rng.randint(1, 265)

    return {
        "VendorID": template["VendorID"],
        "tpep_pickup_datetime": pickup.strftime(DATE_FORMAT),
        "tpep_dropoff_datetime": dropoff.strftime(DATE_FORMAT),
        "passenger_count": str(passengers),
        "trip_distance": f"{distance:.1f}",
        "RatecodeID": template["RatecodeID"],
        "store_and_fwd_flag": template["store_and_fwd_flag"],
        "PULocationID": str(pickup_zone),
        "DOLocationID": str(dropoff_zone),
        "payment_type": template["payment_type"],
        "fare_amount": money(fare),
        "extra": money(extra),
        "mta_tax": money(mta_tax),
        "tip_amount": money(tip),
        "tolls_amount": money(tolls),
        "improvement_surcharge": money(improvement),
        "total_amount": money(total),
        "congestion_surcharge": money(congestion),
    }


def a_tiempo_real(viaje: dict[str, str]) -> dict[str, str]:
    """El viaje acaba de terminar: la llegada es ahora y la recogida, ahora
    menos su duración. (make_row conserva la hora de la plantilla, que en
    rows.csv es casi siempre de madrugada.)"""
    recogida = datetime.strptime(viaje["tpep_pickup_datetime"], DATE_FORMAT)
    llegada = datetime.strptime(viaje["tpep_dropoff_datetime"], DATE_FORMAT)
    ahora = datetime.now().replace(microsecond=0)
    viaje["tpep_dropoff_datetime"] = ahora.strftime(DATE_FORMAT)
    viaje["tpep_pickup_datetime"] = (ahora - (llegada - recogida)).strftime(DATE_FORMAT)
    return viaje


def _post(ruta: str, datos: dict, token: str | None = None) -> dict:
    cabeceras = {"Content-Type": "application/json"}
    if token:
        cabeceras["Authorization"] = f"Bearer {token}"
    peticion = urllib.request.Request(  # noqa: S310 (API_URL se valida al arrancar)
        API_URL + ruta, data=json.dumps(datos).encode(), headers=cabeceras, method="POST"
    )
    with urllib.request.urlopen(peticion, timeout=15) as respuesta:  # noqa: S310
        return json.loads(respuesta.read())


def iniciar_sesion(email: str) -> str:
    return _post("/auth/login", {"email": email, "password": CLAVE})["token"]


def enviar(viaje: dict[str, str], tokens: dict[str, str]) -> str:
    """Envía un viaje y devuelve una línea de resultado para el registro."""
    email = CUENTAS.get(viaje["VendorID"])
    if email is None:
        return f"descartado: VendorID {viaje['VendorID']} sin empresa"
    for _ in range(2):  # segundo intento si el token ha caducado
        try:
            if email not in tokens:
                tokens[email] = iniciar_sesion(email)
            r = _post("/ingesta/viajes", viaje, tokens[email])
            extra = f" anomalías={r['anomalias']}" if r["anomalias"] else ""
            return (f"viaje {r['viaje_id']} -> {r['empresa']} "
                    f"({viaje['total_amount']} $, zona {viaje['PULocationID']}){extra}")
        except urllib.error.HTTPError as error:
            if error.code == 401:
                tokens.pop(email, None)
                continue
            if error.code == 429:
                espera = int(error.headers.get("Retry-After", "5"))
                time.sleep(espera)
                return f"cuota de ingesta superada: esperados {espera} s"
            return f"rechazado ({error.code}): {error.read().decode()[:200]}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            return f"la API no responde ({error}); se reintenta con el siguiente viaje"
    return "no se pudo iniciar sesión"


def main() -> None:
    solo_mostrar = "--solo-mostrar" in sys.argv
    if not API_URL.startswith(("http://", "https://")):
        sys.exit(f"API_URL no válida: {API_URL}")
    plantillas = load_reference(RUTA_CSV)
    rng = random.Random()
    tokens: dict[str, str] = {}
    destino = "solo se muestran" if solo_mostrar else f"se envían a {API_URL}"
    print(f"Simulador en marcha: un viaje cada {INTERVALO_SEG:g} s, {destino} "
          "(Ctrl+C para parar)", flush=True)

    try:
        while True:
            plantilla = rng.choice(plantillas)
            viaje = make_row(plantilla, rng, end_date=date.today(), days_back=0)
            viaje = a_tiempo_real(viaje)
            print(viaje if solo_mostrar else enviar(viaje, tokens), flush=True)
            time.sleep(INTERVALO_SEG)
    except KeyboardInterrupt:
        print("\nSimulador detenido.")


if __name__ == "__main__":
    main()
