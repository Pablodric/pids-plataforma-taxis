#!/usr/bin/env python3
"""
Genera viajes sinteticos a partir de los reales (ingest/datos/rows.csv).

    python scripts/generar_datos_sinteticos.py 10000          # -> ingest/datos/sinteticos.csv
    docker compose down -v                                     # la ingesta solo carga una base vacia
    DATOS_VIAJES=sinteticos.csv docker compose up -d

Cada viaje sintetico copia uno real al azar y lo varia:
  - otro dia de 2020 a la misma hora (el periodo que declara el dataset);
  - distancia, duracion y tarifa escaladas por el mismo factor (+-20 %),
    la propina por otro (+-30 %); recargos, tasas y peajes no cambian
    porque son importes fijos;
  - zonas de recogida y destino tomadas de otros viajes reales, asi que
    los barrios mantienen su peso real;
  - el total se ajusta con la diferencia de sus partes, sin rehacer la
    contabilidad de la TLC (en 2020 el recargo de congestion va a veces
    dentro de 'extra').
Empresa (VendorID), pasajeros y tipo de pago se conservan, de modo que el
reparto entre empresas y las anomalias del original (importes negativos,
distancia cero) mantienen su proporcion: siguen sirviendo para probar E6.
"""

import argparse
import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

DATOS = Path(__file__).resolve().parent.parent / "ingest" / "datos"
FORMATO = "%m/%d/%Y %I:%M:%S %p"


def sintetico(base: dict, reales: list[dict], rng: random.Random) -> dict:
    viaje = dict(base)
    factor = rng.uniform(0.8, 1.2)

    salida = datetime.strptime(base["tpep_pickup_datetime"], FORMATO)
    duracion = datetime.strptime(base["tpep_dropoff_datetime"], FORMATO) - salida
    salida = datetime(2020, 1, 1, salida.hour, salida.minute, salida.second)
    salida += timedelta(days=rng.randrange(366))  # 2020 es bisiesto
    viaje["tpep_pickup_datetime"] = salida.strftime(FORMATO)
    viaje["tpep_dropoff_datetime"] = (salida + duracion * factor).strftime(FORMATO)
    viaje["trip_distance"] = f"{float(base['trip_distance']) * factor:.2f}"

    tarifa = round(float(base["fare_amount"]) * factor, 2)
    propina = round(float(base["tip_amount"]) * rng.uniform(0.7, 1.3), 2)
    diferencia = tarifa - float(base["fare_amount"]) + propina - float(base["tip_amount"])
    viaje["fare_amount"] = f"{tarifa:.2f}"
    viaje["tip_amount"] = f"{propina:.2f}"
    viaje["total_amount"] = f"{float(base['total_amount']) + diferencia:.2f}"

    viaje["PULocationID"] = rng.choice(reales)["PULocationID"]
    viaje["DOLocationID"] = rng.choice(reales)["DOLocationID"]
    return viaje


def generar(filas: int, semilla: int | None = None, origen: Path = DATOS / "rows.csv") -> list[dict]:
    with open(origen, encoding="utf-8") as f:
        reales = list(csv.DictReader(f))
    rng = random.Random(semilla)
    return [sintetico(rng.choice(reales), reales, rng) for _ in range(filas)]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("filas", type=int, help="numero de viajes a generar")
    p.add_argument("--semilla", type=int, default=42, help="misma semilla, mismo fichero (por defecto 42)")
    p.add_argument("--salida", type=Path, default=DATOS / "sinteticos.csv")
    args = p.parse_args()
    if args.filas <= 0:
        p.error("el numero de filas debe ser positivo")

    viajes = generar(args.filas, args.semilla)
    with open(args.salida, "w", encoding="utf-8", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=viajes[0].keys())
        escritor.writeheader()
        escritor.writerows(viajes)
    print(f"{len(viajes)} viajes en {args.salida}")
    print(f"Para cargarlos: docker compose down -v && DATOS_VIAJES={args.salida.name} docker compose up -d")


if __name__ == "__main__":
    main()
