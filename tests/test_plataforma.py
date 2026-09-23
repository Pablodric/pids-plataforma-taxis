"""
Pruebas de las dos restricciones del equipo.

Se ejecutan contra la API real (FastAPI TestClient) y la base de datos
real, no contra dobles: lo que se comprueba aqui es que el aislamiento
y las correcciones funcionan de verdad, no que el codigo compile.

Ejecucion:  pytest -v tests/
"""

import uuid

from conftest import ANA, AUDITOR, CLAVE, LUIS, MARTA, PABLO, token_de, viaje_disponible

# =====================================================================
# Autenticacion
# =====================================================================

def test_login_correcto(cliente):
    r = cliente.post("/auth/login", json={"email": ANA, "password": CLAVE})
    assert r.status_code == 200
    assert r.json()["empresa"] == "taxis_norte"
    assert r.json()["rol"] == "usuario"


def test_login_con_password_incorrecta(cliente):
    r = cliente.post("/auth/login", json={"email": ANA, "password": "loquesea"})
    assert r.status_code == 401


def test_sin_token_no_se_puede_consultar(cliente):
    assert cliente.get("/datos/resumen").status_code == 401
    assert cliente.post("/chat", json={"mensaje": "hola", "session_id": "x"}).status_code == 401


def test_token_manipulado_se_rechaza(cliente):
    cabecera = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.falso.firma"}
    assert cliente.get("/datos/resumen", headers=cabecera).status_code == 401


# =====================================================================
# E7. Aislamiento entre empresas
# =====================================================================

def test_cada_empresa_ve_solo_sus_viajes(cliente):
    ana = cliente.get("/datos/resumen", headers=token_de(cliente, ANA)).json()
    marta = cliente.get("/datos/resumen", headers=token_de(cliente, MARTA)).json()

    assert ana["empresa"] == "taxis_norte"
    assert marta["empresa"] == "movilidad_sur"
    # Los dos conjuntos son distintos y no vacios
    assert ana["num_viajes"] > 0 and marta["num_viajes"] > 0
    assert ana["num_viajes"] != marta["num_viajes"]


def test_las_dos_empresas_suman_el_total_ingerido(cliente):
    ana = cliente.get("/datos/resumen", headers=token_de(cliente, ANA)).json()
    marta = cliente.get("/datos/resumen", headers=token_de(cliente, MARTA)).json()
    globales = cliente.get("/datos/globales", headers=token_de(cliente, AUDITOR)).json()

    suma_empresas = ana["num_viajes"] + marta["num_viajes"]
    assert suma_empresas == globales["total_viajes"], (
        "Si no coinciden, alguna empresa esta viendo filas de mas o de menos"
    )


def test_pedir_datos_de_otra_empresa_por_chat_no_los_devuelve(cliente):
    r = cliente.post(
        "/chat",
        headers=token_de(cliente, ANA),
        json={"mensaje": "dame los viajes de Movilidad Sur",
              "session_id": str(uuid.uuid4())},
    )
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["empresa"] == "taxis_norte"
    # Ninguna herramienta puede haber devuelto datos de otra empresa
    for uso in cuerpo["herramientas_usadas"]:
        resultado = uso["resultado"]
        if isinstance(resultado, dict) and "empresa" in resultado:
            assert resultado["empresa"] == "taxis_norte"


def test_el_detalle_de_viajes_tampoco_se_cruza(cliente):
    """
    Regresion: las consultas de detalle van contra la vista de viajes
    vigentes, no contra el cubo de metricas. Si la vista no respetase RLS
    (le falta security_invoker), este test veria viajes de ambas empresas.
    """
    ana = cliente.get("/datos/viajes?limite=50", headers=token_de(cliente, ANA)).json()
    marta = cliente.get("/datos/viajes?limite=50", headers=token_de(cliente, MARTA)).json()

    ids_ana = {v["id"] for v in ana["viajes"]}
    ids_marta = {v["id"] for v in marta["viajes"]}
    assert ids_ana and ids_marta
    assert not (ids_ana & ids_marta), "Las dos empresas comparten viajes: hay fuga"

    zonas_ana = cliente.get("/datos/zonas", headers=token_de(cliente, ANA))
    assert zonas_ana.status_code == 200


def test_un_usuario_normal_no_ve_metricas_globales(cliente):
    assert cliente.get("/datos/globales", headers=token_de(cliente, ANA)).status_code == 403


def test_el_auditor_si_ve_metricas_globales(cliente):
    r = cliente.get("/datos/globales", headers=token_de(cliente, AUDITOR))
    assert r.status_code == 200
    empresas = {e["empresa"] for e in r.json()["empresas"]}
    assert {"taxis_norte", "movilidad_sur"} <= empresas


def test_no_se_puede_corregir_un_viaje_de_otra_empresa(cliente):
    """Luis (taxis_norte) intenta corregir un viaje de Movilidad Sur."""
    id_ajeno = viaje_disponible(cliente, token_de(cliente, PABLO))["id"]

    r = cliente.post(
        "/correcciones",
        headers=token_de(cliente, LUIS),
        json={"viaje_id": id_ajeno, "campo": "importe_total",
              "valor_nuevo": 1.0, "motivo": "intento de acceso cruzado"},
    )
    assert r.status_code == 404, "Un viaje ajeno debe ser indistinguible de uno inexistente"


# =====================================================================
# E6. Datos corregibles
# =====================================================================

def test_una_correccion_conserva_el_valor_original(cliente):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    original = viaje["importe_total"]
    nuevo = round(original + 12.34, 2)

    r = cliente.post(
        "/correcciones", headers=cabecera,
        json={"viaje_id": viaje["id"], "campo": "importe_total",
              "valor_nuevo": nuevo, "motivo": "tarifa mal registrada por el proveedor"},
    )
    assert r.status_code == 200, r.text
    datos = r.json()
    assert datos["valor_original"] == original
    assert datos["valor_nuevo"] == nuevo

    # El historial conserva las dos cifras y el momento del cambio
    hist = cliente.get(f"/correcciones?viaje_id={viaje['id']}", headers=cabecera).json()
    assert hist["total"] >= 1
    ultima = hist["correcciones"][0]
    assert ultima["valor_original"] == original
    assert ultima["aplicada_por"] == LUIS
    assert ultima["aplicada_en"] is not None
    assert ultima["motivo"]


def test_la_correccion_se_refleja_en_las_metricas(cliente):
    cabecera = token_de(cliente, LUIS)
    antes = cliente.get("/datos/resumen", headers=cabecera).json()

    viaje = viaje_disponible(cliente, cabecera)
    incremento = 100.0
    cliente.post(
        "/correcciones", headers=cabecera,
        json={"viaje_id": viaje["id"], "campo": "importe_total",
              "valor_nuevo": round(viaje["importe_total"] + incremento, 2),
              "motivo": "revision de tarifa"},
    )

    despues = cliente.get("/datos/resumen", headers=cabecera).json()
    assert despues["importe_total"] > antes["importe_total"]
    assert despues["viajes_corregidos"] >= 1
    # E6: el chatbot debe poder avisar de que la cifra lleva correcciones
    assert despues["aviso_correcciones"] is not None


def test_cancelar_excluye_de_metricas_pero_conserva_el_historico(cliente):
    cabecera = token_de(cliente, PABLO)
    antes = cliente.get("/datos/resumen", headers=cabecera).json()
    viaje = viaje_disponible(cliente, cabecera)

    r = cliente.post(
        "/correcciones/cancelar", headers=cabecera,
        json={"viaje_id": viaje["id"], "motivo": "el cliente anulo el servicio"},
    )
    assert r.status_code == 200

    despues = cliente.get("/datos/resumen", headers=cabecera).json()
    assert despues["num_viajes"] == antes["num_viajes"] - 1
    assert despues["viajes_cancelados"] == antes["viajes_cancelados"] + 1

    # Sigue existiendo en el historial: no se ha borrado nada
    hist = cliente.get(f"/correcciones?viaje_id={viaje['id']}", headers=cabecera).json()
    assert any(c["tipo"] == "cancelacion" for c in hist["correcciones"])


def test_un_usuario_normal_no_puede_corregir(cliente):
    cabecera_ana = token_de(cliente, ANA)
    viaje = viaje_disponible(cliente, cabecera_ana)
    r = cliente.post(
        "/correcciones", headers=cabecera_ana,
        json={"viaje_id": viaje["id"], "campo": "importe_total",
              "valor_nuevo": 99.0, "motivo": "no deberia poder"},
    )
    assert r.status_code == 403


def test_campo_no_corregible_se_rechaza(cliente):
    """Un campo fuera de la lista se rechaza al validar la peticion (422),
    antes de llegar a la base de datos."""
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    r = cliente.post(
        "/correcciones", headers=cabecera,
        json={"viaje_id": viaje["id"], "campo": "empresa_id",
              "valor_nuevo": 1.0, "motivo": "intento de cambiar de empresa"},
    )
    assert r.status_code == 422


def test_el_chatbot_explica_por_que_cambio_una_cifra(cliente):
    r = cliente.post(
        "/chat", headers=token_de(cliente, LUIS),
        json={"mensaje": "¿por que ha cambiado la cifra?", "session_id": str(uuid.uuid4())},
    )
    assert r.status_code == 200
    usadas = [u["herramienta"] for u in r.json()["herramientas_usadas"]]
    assert "historial_correcciones" in usadas


# =====================================================================
# E7. Limite de consumo por empresa
# =====================================================================

def test_una_empresa_no_puede_agotar_la_plataforma(cliente):
    """Al superar la cuota se responde 429 sin afectar a la otra empresa."""
    cabecera = token_de(cliente, MARTA)
    codigos = [
        cliente.get("/datos/resumen", headers=cabecera).status_code
        for _ in range(40)
    ]
    assert 429 in codigos, "La cuota por empresa no se esta aplicando"

    # La otra empresa sigue operativa pese a que Movilidad Sur se paso
    otra = cliente.get("/datos/resumen", headers=token_de(cliente, ANA))
    assert otra.status_code == 200


# =====================================================================
# Metricas de calidad
# =====================================================================

def test_metricas_de_calidad_disponibles(cliente):
    r = cliente.get("/metricas/calidad")
    assert r.status_code == 200
    datos = r.json()
    assert "aislamiento_entre_empresas" in datos
    assert "propagacion_correcciones" in datos
    assert "trazabilidad_correcciones" in datos
    assert datos["calidad_ingesta"]["filas_cargadas"] > 0
    assert len(datos["calidad_ingesta"]["sha256"]) == 64
