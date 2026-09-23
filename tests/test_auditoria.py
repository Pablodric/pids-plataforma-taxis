"""
Viaje en el tiempo, cadena de hashes a prueba de manipulaciones e
invariante del cubo incremental.
"""

import os
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from app import repositorio
from conftest import AUDITOR, LUIS, MARTA, PABLO, token_de, viaje_disponible


def corregir(cliente, cabecera, viaje_id, campo, valor, motivo="prueba de auditoría"):
    r = cliente.post("/correcciones", headers=cabecera,
                     json={"viaje_id": viaje_id, "campo": campo, "valor_nuevo": valor,
                           "motivo": motivo})
    assert r.status_code == 200, r.text
    return r.json()


def en(cliente, cabecera, instante: str):
    r = cliente.get("/datos/en", headers=cabecera, params={"instante": instante})
    assert r.status_code == 200, r.text
    return r.json()


def justo_antes(iso: str) -> str:
    return (datetime.fromisoformat(iso) - timedelta(microseconds=1)).isoformat()


# =====================================================================
# Viaje en el tiempo
# =====================================================================

def test_las_cifras_de_antes_de_una_correccion_se_reconstruyen(cliente):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    antes = cliente.get("/datos/resumen", headers=cabecera).json()
    hecho = corregir(cliente, cabecera, viaje["id"], "importe_total",
                     round(viaje["importe_total"] + 100, 2))

    pasado = en(cliente, cabecera, justo_antes(hecho["aplicada_en"]))
    assert pasado["en_ese_instante"]["importe_total"] == antes["importe_total"]
    assert pasado["diferencia"]["importe_total"] == pytest.approx(100, abs=0.01)
    assert pasado["correcciones_posteriores"] >= 1
    # Y en el instante de la correccion ya cuenta
    assert en(cliente, cabecera, hecho["aplicada_en"])["diferencia"]["importe_total"] == 0


def test_antes_de_una_correccion_por_su_id_y_aislado(cliente):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    hecho = corregir(cliente, cabecera, viaje["id"], "importe_total",
                     round(viaje["importe_total"] + 10, 2))
    r = cliente.get("/datos/en", headers=cabecera,
                    params={"antes_de_correccion": hecho["correccion_id"]})
    assert r.status_code == 200 and r.json()["diferencia"]["importe_total"] >= 10 - 0.01
    # Otra empresa no puede ni saber cuando se hizo esa correccion
    ajena = cliente.get("/datos/en", headers=token_de(cliente, MARTA),
                        params={"antes_de_correccion": hecho["correccion_id"]})
    assert ajena.status_code == 404


def test_una_cancelacion_se_ve_en_el_tiempo(cliente):
    cabecera = token_de(cliente, PABLO)
    viaje = viaje_disponible(cliente, cabecera)
    r = cliente.post("/correcciones/cancelar", headers=cabecera,
                     json={"viaje_id": viaje["id"], "motivo": "anulado para la prueba"}).json()
    pasado = en(cliente, cabecera, justo_antes(r["aplicada_en"]))
    assert pasado["diferencia"]["num_viajes"] == -1
    assert pasado["diferencia"]["viajes_cancelados"] == 1


def test_el_pasado_tambien_esta_aislado_por_empresa(cliente):
    """La reconstruccion va por la misma RLS: el 'ahora' historico de una
    empresa coincide con su resumen, sin viajes de la otra."""
    cabecera = token_de(cliente, MARTA)
    ahora = en(cliente, cabecera, datetime.now().astimezone().isoformat())["ahora"]
    resumen = cliente.get("/datos/resumen", headers=cabecera).json()
    assert ahora["num_viajes"] == resumen["num_viajes"]
    assert ahora["importe_total"] == pytest.approx(resumen["importe_total"], abs=0.01)


def test_antes_de_la_ingesta_no_habia_nada(cliente):
    pasado = en(cliente, token_de(cliente, LUIS), "2000-01-01T00:00:00Z")
    assert pasado["en_ese_instante"]["num_viajes"] == 0


def test_instante_no_valido(cliente):
    r = cliente.get("/datos/en", headers=token_de(cliente, LUIS), params={"instante": "ayer"})
    assert r.status_code == 400


# =====================================================================
# Cadena de hashes
# =====================================================================

def test_la_cadena_es_integra_y_crece_con_cada_correccion(cliente):
    cabecera = token_de(cliente, LUIS)
    antes = cliente.get("/auditoria/cadena", headers=cabecera).json()
    propia = antes["cadenas"][0]
    assert antes["integra"] is True and propia["empresa"] == "taxis_norte"

    viaje = viaje_disponible(cliente, cabecera)
    corregir(cliente, cabecera, viaje["id"], "propina", 2.0)
    despues = cliente.get("/auditoria/cadena", headers=cabecera).json()["cadenas"][0]
    assert despues["integra"] is True
    assert despues["eslabones"] == propia.get("eslabones", 0) + 1
    assert despues["cabeza"] != propia.get("cabeza")


def test_cada_empresa_verifica_solo_su_cadena_y_el_auditor_todas(cliente):
    empresas = [c["empresa"] for c in
                cliente.get("/auditoria/cadena", headers=token_de(cliente, MARTA)).json()["cadenas"]]
    assert empresas == ["movilidad_sur"]
    todas = cliente.get("/auditoria/cadena", headers=token_de(cliente, AUDITOR)).json()
    assert {"movilidad_sur", "taxis_norte"} <= {c["empresa"] for c in todas["cadenas"]}


def test_la_verificacion_detecta_borrados_y_modificaciones():
    """Sobre una cadena construida en memoria con el mismo algoritmo."""
    import hashlib
    from decimal import Decimal

    filas, previo = [], repositorio.GENESIS
    for n in range(1, 4):
        f = {"id": n, "empresa_id": "e", "viaje_id": n, "tipo": "correccion",
             "campo": "propina", "valor_original": Decimal("1.00"), "valor_nuevo": Decimal("2.50"),
             "motivo": "m", "aplicada_por": "x", "eslabon": n, "hash_anterior": previo,
             "aplicada_en": datetime(2026, 1, 1, tzinfo=UTC)}
        f["hash"] = hashlib.sha256(repositorio.contenido_canonico(f).encode()).hexdigest()
        filas.append(f)
        previo = f["hash"]

    assert repositorio.verificar_eslabones(filas)["integra"] is True
    borrada = repositorio.verificar_eslabones([filas[0], filas[2]])
    assert borrada == {"integra": False, "eslabon": 2, "problema": "falta el eslabón 2 (borrado)"}
    alterada = [dict(f) for f in filas]
    alterada[1]["valor_nuevo"] = Decimal("99.00")
    r = repositorio.verificar_eslabones(alterada)
    assert r["integra"] is False and r["eslabon"] == 2 and "modificado" in r["problema"]


ADMIN = os.environ.get("DATABASE_URL_ADMIN")


@pytest.mark.skipif(not ADMIN, reason="requiere DATABASE_URL_ADMIN (superusuario)")
def test_una_manipulacion_como_superusuario_se_detecta(cliente):
    """
    El ataque que ningun permiso puede impedir: un superusuario desactiva
    los disparadores y cambia un valor ya registrado. La cadena lo delata.
    """
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    hecho = corregir(cliente, cabecera, viaje["id"], "propina", 3.33, "valor a manipular")
    cid = hecho["correccion_id"]

    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute("ALTER TABLE correcciones DISABLE TRIGGER USER")
        try:
            conn.execute("UPDATE correcciones SET valor_nuevo = 0.01 WHERE id = %s", (cid,))
            r = cliente.get("/auditoria/cadena", headers=cabecera).json()
            assert r["integra"] is False
            fallo = r["cadenas"][0]
            assert fallo.get("correccion_id") == cid and "modificado" in fallo["problema"]
        finally:
            conn.execute("UPDATE correcciones SET valor_nuevo = 3.33 WHERE id = %s", (cid,))
            conn.execute("ALTER TABLE correcciones ENABLE TRIGGER USER")

    assert cliente.get("/auditoria/cadena", headers=cabecera).json()["integra"] is True


# =====================================================================
# Invariante del cubo incremental (se ejecuta tras muchas correcciones)
# =====================================================================

def test_el_cubo_incremental_coincide_con_un_recalculo_completo(cliente):
    viaje = viaje_disponible(cliente, token_de(cliente, LUIS))
    corregir(cliente, token_de(cliente, LUIS), viaje["id"], "importe_total", 12.34)
    c = repositorio.consistencia_cubo()
    assert c["cubos_comparados"] > 0
    assert c["consistente"] is True, c
    assert cliente.get("/metricas/calidad").json()["consistencia_cubo"]["consistente"] is True


# =====================================================================
# Observabilidad y freno a la fuerza bruta
# =====================================================================

def test_metricas_prometheus_con_rutas_como_plantilla(cliente):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    cliente.get(f"/datos/viajes/{viaje['id']}", headers=cabecera)
    texto = cliente.get("/metrics").text
    assert "pids_http_duracion_segundos_bucket" in texto
    assert 'ruta="/datos/viajes/{viaje_id}"' in texto
    assert f'/datos/viajes/{viaje["id"]}"' not in texto, "ids en etiquetas = cardinalidad sin límite"
    assert "pids_correcciones_total" in texto


def test_request_id_se_genera_o_se_respeta(cliente):
    assert len(cliente.get("/salud").headers["X-Request-ID"]) == 16
    assert cliente.get("/salud", headers={"X-Request-ID": "abc123"}).headers["X-Request-ID"] == "abc123"


def test_login_se_bloquea_tras_cinco_fallos_y_solo_para_ese_correo(cliente):
    for _ in range(5):
        assert cliente.post("/auth/login", json={"email": MARTA, "password": "mal"}).status_code == 401
    bloqueado = cliente.post("/auth/login", json={"email": MARTA, "password": "demo1234"})
    assert bloqueado.status_code == 429 and "Retry-After" in bloqueado.headers
    # Otro usuario entra sin problema
    assert cliente.post("/auth/login", json={"email": PABLO, "password": "demo1234"}).status_code == 200


def test_un_login_correcto_reinicia_el_contador(cliente):
    for _ in range(4):
        cliente.post("/auth/login", json={"email": LUIS, "password": "mal"})
    assert cliente.post("/auth/login", json={"email": LUIS, "password": "demo1234"}).status_code == 200
    for _ in range(4):
        cliente.post("/auth/login", json={"email": LUIS, "password": "mal"})
    assert cliente.post("/auth/login", json={"email": LUIS, "password": "demo1234"}).status_code == 200


def test_el_chat_responde_con_las_cifras_de_antes_de_la_ultima_correccion(cliente):
    import uuid
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    corregir(cliente, cabecera, viaje["id"], "importe_total", round(viaje["importe_total"] + 7, 2))
    r = cliente.post("/chat", headers=cabecera, json={
        "mensaje": "¿cuánto facturábamos antes de la última corrección?",
        "session_id": str(uuid.uuid4())}).json()
    uso = r["herramientas_usadas"][0]
    assert uso["herramienta"] == "metricas_en_el_tiempo"
    assert uso["resultado"]["diferencia"]["importe_total"] == pytest.approx(7, abs=0.01)
    assert "reconstruido desde el log" in r["respuesta"]
