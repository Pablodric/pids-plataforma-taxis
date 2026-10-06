"""
Ingesta en tiempo real (POST /ingesta/viajes).

Se comprueba lo que exigen las restricciones: la empresa del viaje sale de
la credencial (E7), solo la cuenta de proveedor puede enviar, la ingesta
tiene su propia cuota por empresa, y las metricas se actualizan de forma
incremental sin dejar de cuadrar con el recalculo completo (E6).
"""

import psycopg
import pytest
from app import config, db
from conftest import ANA, AUDITOR, LUIS, MARTA, token_de

SISTEMA_NORTE = "sistema@taxisnorte.es"
SISTEMA_SUR = "sistema@movilidadsur.es"


def viaje(**cambios):
    base = {
        "VendorID": 1,
        "tpep_pickup_datetime": "10/05/2026 03:10:00 PM",
        "tpep_dropoff_datetime": "10/05/2026 03:25:00 PM",
        "passenger_count": "1", "trip_distance": "2.4",
        "PULocationID": "161", "DOLocationID": "237", "payment_type": "1",
        "fare_amount": "11.00", "tip_amount": "2.50", "tolls_amount": "0.00",
        "congestion_surcharge": "2.50", "total_amount": "17.30",
        # columnas del CSV que la plataforma no guarda: se aceptan y se ignoran
        "RatecodeID": "1", "store_and_fwd_flag": "N", "extra": "0.5",
        "mta_tax": "0.5", "improvement_surcharge": "0.3",
    }
    base.update(cambios)
    return base


def resumen(cliente, email):
    return cliente.get("/datos/resumen", headers=token_de(cliente, email)).json()


def test_un_viaje_nuevo_entra_y_actualiza_las_metricas(cliente):
    antes = resumen(cliente, ANA)
    r = cliente.post("/ingesta/viajes", json=viaje(), headers=token_de(cliente, SISTEMA_NORTE))
    assert r.status_code == 201, r.text
    assert r.json()["empresa"] == "taxis_norte" and r.json()["anomalias"] == []
    despues = resumen(cliente, ANA)
    assert despues["num_viajes"] == antes["num_viajes"] + 1
    assert despues["importe_total"] == pytest.approx(antes["importe_total"] + 17.30, abs=0.01)


def test_el_viaje_nuevo_no_lo_ve_la_otra_empresa(cliente):
    antes = resumen(cliente, MARTA)
    r = cliente.post("/ingesta/viajes", json=viaje(), headers=token_de(cliente, SISTEMA_NORTE))
    assert r.status_code == 201
    assert resumen(cliente, MARTA)["num_viajes"] == antes["num_viajes"]
    ficha = cliente.get(f"/datos/viajes/{r.json()['viaje_id']}", headers=token_de(cliente, MARTA))
    assert ficha.status_code == 404


def test_la_empresa_sale_de_la_credencial_no_del_dato(cliente):
    """Un proveedor no puede colar viajes a otra empresa cambiando el VendorID."""
    antes_sur, antes_norte = resumen(cliente, MARTA), resumen(cliente, ANA)
    r = cliente.post("/ingesta/viajes", json=viaje(VendorID=1),
                     headers=token_de(cliente, SISTEMA_SUR))
    assert r.status_code == 201 and r.json()["empresa"] == "movilidad_sur"
    assert resumen(cliente, MARTA)["num_viajes"] == antes_sur["num_viajes"] + 1
    assert resumen(cliente, ANA)["num_viajes"] == antes_norte["num_viajes"]


@pytest.mark.parametrize("email", [ANA, LUIS, AUDITOR])
def test_solo_la_cuenta_de_proveedor_puede_enviar_viajes(cliente, email):
    r = cliente.post("/ingesta/viajes", json=viaje(), headers=token_de(cliente, email))
    assert r.status_code == 403


def test_sin_token_no_se_ingiere(cliente):
    assert cliente.post("/ingesta/viajes", json=viaje()).status_code == 401


def test_la_cuenta_de_proveedor_no_puede_consultar_ni_corregir(cliente):
    cab = token_de(cliente, SISTEMA_NORTE)
    assert cliente.get("/datos/panel", headers=cab).status_code == 403
    assert cliente.post("/chat", json={"mensaje": "hola", "session_id": "s"},
                        headers=cab).status_code == 403
    assert cliente.post("/correcciones", headers=cab, json={
        "viaje_id": 1, "campo": "propina", "valor_nuevo": 1, "motivo": "prueba"}).status_code == 403


@pytest.mark.parametrize("cambios", [
    {"tpep_pickup_datetime": "ayer por la tarde"},
    {"PULocationID": "999"},
    {"total_amount": "150000"},
    {"passenger_count": "40"},
])
def test_un_viaje_mal_formado_se_rechaza(cliente, cambios):
    r = cliente.post("/ingesta/viajes", json=viaje(**cambios),
                     headers=token_de(cliente, SISTEMA_NORTE))
    assert r.status_code == 422, r.text


def test_las_anomalias_se_cargan_y_se_avisan(cliente):
    r = cliente.post("/ingesta/viajes", headers=token_de(cliente, SISTEMA_NORTE),
                     json=viaje(trip_distance="0", total_amount="-4.00",
                                tpep_dropoff_datetime="10/05/2026 03:00:00 PM"))
    assert r.status_code == 201
    assert set(r.json()["anomalias"]) == {"distancia_cero", "importe_negativo",
                                          "llegada_anterior_a_salida"}


def test_la_bd_impide_insertar_un_viaje_a_nombre_de_otra_empresa(cliente):
    """Aunque fallara la API: la politica RLS exige que el viaje sea de la
    empresa de la sesion."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege), \
            db.conexion_empresa("taxis_norte", "proveedor") as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO viajes (empresa_id, pickup_ts, pu_location_id, importe_total)
                       VALUES ('movilidad_sur', now(), 161, 10)""")


@pytest.mark.parametrize("rol", ["usuario", "operador", "auditor"])
def test_la_bd_impide_insertar_viajes_a_quien_no_es_proveedor(cliente, rol):
    with pytest.raises(psycopg.errors.InsufficientPrivilege), \
            db.conexion_empresa("taxis_norte", rol) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO viajes (empresa_id, pickup_ts, pu_location_id, importe_total)
                       VALUES ('taxis_norte', now(), 161, 10)""")


def test_la_ingesta_tiene_cuota_propia_por_empresa(cliente, monkeypatch):
    """Agotar la cuota de ingesta de una empresa no afecta a la otra ni a
    las consultas de sus propios usuarios."""
    monkeypatch.setattr(config, "CUOTA_INGESTA", 3)
    norte, sur = token_de(cliente, SISTEMA_NORTE), token_de(cliente, SISTEMA_SUR)
    codigos = [cliente.post("/ingesta/viajes", json=viaje(), headers=norte).status_code
               for _ in range(5)]
    assert codigos == [201, 201, 201, 429, 429]
    assert cliente.post("/ingesta/viajes", json=viaje(), headers=sur).status_code == 201
    assert cliente.get("/datos/resumen", headers=token_de(cliente, ANA)).status_code == 200


def test_el_cubo_sigue_cuadrando_tras_ingerir_y_corregir(cliente):
    r = cliente.post("/ingesta/viajes", json=viaje(), headers=token_de(cliente, SISTEMA_NORTE))
    nuevo = r.json()["viaje_id"]
    c = cliente.post("/correcciones", headers=token_de(cliente, LUIS), json={
        "viaje_id": nuevo, "campo": "importe_total", "valor_nuevo": 21.5,
        "motivo": "tarifa mal registrada"})
    assert c.status_code == 200, c.text
    assert cliente.get("/metricas/calidad").json()["consistencia_cubo"]["consistente"] is True
