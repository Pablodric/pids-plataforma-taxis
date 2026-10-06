"""
Ingesta inicial de la plataforma.

Carga en Postgres:
  1. El catalogo de zonas de NYC (fuente externa, 265 zonas).
  2. Las empresas y sus usuarios de demostracion.
  3. El historico de viajes del CSV, asignando la empresa propietaria
     DESDE LA INGESTA (requisito explicito de E7).
  4. El agregado inicial de metricas.

Es idempotente: si los datos ya estan cargados, no hace nada.
Se ejecuta como superusuario, asi que no le afecta el RLS.
"""

import csv
import hashlib
import json
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DIR_DATOS = Path(__file__).parent / "datos"
DSN = os.environ["DATABASE_URL_ADMIN"]

# Fichero de viajes dentro de datos/. Por defecto el real; con
# DATOS_VIAJES=sinteticos.csv, el de scripts/generar_datos_sinteticos.py.
FICHERO_VIAJES = Path(os.environ.get("DATOS_VIAJES") or "rows.csv").name

# ---------------------------------------------------------------------
# E7: mapeo VendorID -> empresa propietaria.
# El dataset publico de la TLC identifica al proveedor tecnologico con
# VendorID; lo usamos como identidad de la empresa, que es justo lo que
# pide E7 ("identificar la empresa propietaria de cada evento desde la
# ingesta"). En un entorno real este dato vendria del contrato de
# ingesta de cada empresa (cabecera, topic Kafka propio, etc.).
# ---------------------------------------------------------------------
EMPRESAS = [
    # (id, nombre, vendor_id, cuota consultas/min)
    ("taxis_norte", "Taxis del Norte S.L.", 1, 20),
    ("movilidad_sur", "Movilidad Sur S.A.", 2, 20),
    ("plataforma", "Operador de la plataforma", None, 60),
]

USUARIOS = [
    # (email, empresa, rol, password)
    ("ana@taxisnorte.es", "taxis_norte", "usuario", "demo1234"),
    ("luis@taxisnorte.es", "taxis_norte", "operador", "demo1234"),
    ("marta@movilidadsur.es", "movilidad_sur", "usuario", "demo1234"),
    ("pablo@movilidadsur.es", "movilidad_sur", "operador", "demo1234"),
    ("auditor@plataforma.es", "plataforma", "auditor", "demo1234"),
    # Cuentas de maquina: con ellas cada empresa envia sus viajes en tiempo
    # real. La empresa del viaje sale de esta credencial, no del dato.
    ("sistema@taxisnorte.es", "taxis_norte", "proveedor", "demo1234"),
    ("sistema@movilidadsur.es", "movilidad_sur", "proveedor", "demo1234"),
]


def hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-HMAC-SHA256 con sal aleatoria. Formato: pbkdf2$<sal>$<hash>."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def esperar_postgres(intentos: int = 30) -> None:
    for i in range(intentos):
        try:
            with psycopg.connect(DSN, connect_timeout=3):
                print("[ingesta] Postgres disponible")
                return
        except Exception:
            print(f"[ingesta] esperando a Postgres ({i + 1}/{intentos})...")
            time.sleep(2)
    sys.exit("[ingesta] Postgres no responde")


def parsear_fecha(valor: str):
    """El CSV de la TLC usa 'MM/DD/YYYY hh:mm:ss AM'."""
    valor = (valor or "").strip()
    if not valor:
        return None
    for formato in ("%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(valor, formato)
        except ValueError:
            continue
    return None


def numero(valor, tipo=float):
    try:
        if valor is None or str(valor).strip() == "":
            return None
        return tipo(valor)
    except (TypeError, ValueError):
        return None


def cargar_zonas(cur) -> int:
    ruta = DIR_DATOS / "taxi_zone_lookup.csv"
    filas = []
    with open(ruta, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            filas.append((
                int(fila["LocationID"]),
                fila["Borough"],
                fila["Zone"],
                fila["service_zone"],
            ))
    cur.executemany(
        "INSERT INTO zonas (location_id, borough, zona, service_zone) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (location_id) DO NOTHING",
        filas,
    )
    return len(filas)


def cargar_empresas_y_usuarios(cur) -> None:
    cur.executemany(
        "INSERT INTO empresas (id, nombre, vendor_id_origen, cuota_consultas_min) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        EMPRESAS,
    )
    cur.executemany(
        "INSERT INTO usuarios (email, empresa_id, rol, password_hash) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING",
        [(e, emp, rol, hash_password(pw)) for e, emp, rol, pw in USUARIOS],
    )


def sha256_de(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


# Periodo que declara el dataset ("2020 Yellow Taxi Trip Data"). Los
# viajes fuera de el son un problema conocido de calidad del fichero de
# la TLC (taximetros con la fecha mal puesta).
PERIODO_DATASET = (datetime(2020, 1, 1), datetime(2021, 1, 1))


def detectar_anomalias(pickup, dropoff, importe, distancia) -> list[str]:
    """
    Anomalias de calidad. NO se descartan: son hechos tal y como los
    envio el proveedor, y precisamente E6 existe para corregirlos
    despues. Se cuentan y quedan registradas en la tabla 'ingestas'.
    """
    anomalias = []
    if not (PERIODO_DATASET[0] <= pickup < PERIODO_DATASET[1]):
        anomalias.append("fecha_fuera_de_periodo")
    if dropoff is not None and dropoff < pickup:
        anomalias.append("llegada_anterior_a_salida")
    if importe is not None and importe < 0:
        anomalias.append("importe_negativo")
    if distancia is not None and distancia == 0:
        anomalias.append("distancia_cero")
    return anomalias


def cargar_viajes(cur) -> dict:
    por_vendor = {v: emp for emp, _, v, _ in EMPRESAS if v is not None}
    ruta = DIR_DATOS / FICHERO_VIAJES
    print(f"[ingesta] viajes de {ruta.name}")
    filas, descartadas, leidas = [], 0, 0
    anomalias: dict[str, int] = {}

    with open(ruta, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            leidas += 1
            vendor = numero(fila.get("VendorID"), int)
            empresa = por_vendor.get(vendor)
            pickup = parsear_fecha(fila.get("tpep_pickup_datetime"))
            # Sin empresa propietaria o sin fecha, el evento no se ingiere:
            # E7 exige que todo hecho tenga dueno identificado.
            if empresa is None or pickup is None:
                descartadas += 1
                continue
            dropoff = parsear_fecha(fila.get("tpep_dropoff_datetime"))
            importe = numero(fila.get("total_amount"))
            distancia = numero(fila.get("trip_distance"))
            for a in detectar_anomalias(pickup, dropoff, importe, distancia):
                anomalias[a] = anomalias.get(a, 0) + 1
            filas.append((
                empresa,
                vendor,
                pickup,
                dropoff,
                numero(fila.get("passenger_count"), int),
                distancia,
                numero(fila.get("PULocationID"), int),
                numero(fila.get("DOLocationID"), int),
                numero(fila.get("payment_type"), int),
                numero(fila.get("fare_amount")),
                numero(fila.get("tip_amount")),
                numero(fila.get("tolls_amount")),
                numero(fila.get("congestion_surcharge")),
                importe,
            ))

    # Carga masiva: el disparador de viajes no recalcula fila a fila; los
    # cubos se calculan una sola vez al final con recalcular_todo().
    cur.execute("SELECT set_config('app.carga_masiva', '1', true)")
    cur.executemany(
        """INSERT INTO viajes (
               empresa_id, vendor_id, pickup_ts, dropoff_ts, pasajeros,
               distancia, pu_location_id, do_location_id, tipo_pago,
               importe_base, propina, peajes, recargo_congestion, importe_total)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        filas,
    )
    cur.execute(
        """INSERT INTO ingestas (fichero, sha256, filas_leidas, filas_cargadas,
                                 filas_descartadas, anomalias)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (ruta.name, sha256_de(ruta), leidas, len(filas), descartadas,
         json.dumps(anomalias)),
    )
    if descartadas:
        print(f"[ingesta] {descartadas} filas descartadas (sin empresa o sin fecha)")
    for tipo, n in sorted(anomalias.items()):
        print(f"[ingesta]   anomalia '{tipo}': {n} viajes (se cargan y se pueden corregir)")
    return {"cargadas": len(filas), "descartadas": descartadas}


def main() -> None:
    esperar_postgres()
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM viajes")
            if cur.fetchone()["n"] > 0:
                print("[ingesta] los datos ya estaban cargados, no se hace nada")
                return

            n_zonas = cargar_zonas(cur)
            print(f"[ingesta] {n_zonas} zonas de NYC cargadas")

            cargar_empresas_y_usuarios(cur)
            print(f"[ingesta] {len(EMPRESAS)} empresas y {len(USUARIOS)} usuarios")

            resultado = cargar_viajes(cur)
            print(f"[ingesta] {resultado['cargadas']} viajes cargados")

            cur.execute("SELECT recalcular_todo() AS cubos")
            print(f"[ingesta] {cur.fetchone()['cubos']} cubos de metricas calculados")

            cur.execute(
                "SELECT empresa_id, COUNT(*) AS n FROM viajes GROUP BY empresa_id ORDER BY 1"
            )
            for fila in cur.fetchall():
                print(f"[ingesta]   {fila['empresa_id']}: {fila['n']} viajes")
        conn.commit()
    print("[ingesta] completada")


if __name__ == "__main__":
    main()
