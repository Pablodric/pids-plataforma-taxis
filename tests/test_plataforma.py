"""
Pruebas de las dos restricciones del equipo.

Se ejecutan contra la API real (FastAPI TestClient) y la base de datos
real, no contra dobles: lo que se comprueba aqui es que el aislamiento
y las correcciones funcionan de verdad, no que el codigo compile.

Ejecucion:  pytest -v tests/
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app

CLAVE = "demo1234"
ANA = "ana@taxisnorte.es"          # taxis_norte, usuario
LUIS = "luis@taxisnorte.es"        # taxis_norte, operador
MARTA = "marta@movilidadsur.es"    # movilidad_sur, usuario
PABLO = "pablo@movilidadsur.es"    # movilidad_sur, operador
AUDITOR = "auditor@plataforma.es"  # plataforma, auditor


@pytest.fixture(scope="module")
def cliente():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def limpiar_cuotas(cliente):
    """
    Cada prueba arranca con la cuota de todas las empresas a cero.
    Sin esto las pruebas se estorban entre si: el limitador es por
    empresa, no por prueba, y una prueba previa agotaria la cuota de
    la siguiente.
    """
    from app import db

    for clave in db.cache().scan_iter("cuota:*"):
        db.cache().delete(clave)
    yield

def viaje_disponible(cliente, cabecera):
    """Primer viaje ni cancelado ni corregido: las pruebas no deben
    depender del estado que hayan dejado las anteriores."""
    viajes = cliente.get("/datos/viajes?limite=50", headers=cabecera).json()["viajes"]
    for v in viajes:
        if not v["cancelado"] and not v["corregido"]:
            return v
    for v in viajes:
        if not v["cancelado"]:
            return v
    raise AssertionError("No hay viajes disponibles para la prueba")


def token_de(cliente, email):
    r = cliente.post("/auth/login", json={"email": email, "password": CLAVE})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


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
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    r = cliente.post(
        "/correcciones", headers=cabecera,
        json={"viaje_id": viaje["id"], "campo": "empresa_id",
              "valor_nuevo": 1.0, "motivo": "intento de cambiar de empresa"},
    )
    assert r.status_code == 404


def test_el_chatbot_explica_por_que_cambio_una_cifra(cliente):
    """
    La pregunta "por que ha cambiado" debe acabar consultando el historial
    de correcciones. Se comprueba el NLU de reglas directamente para que
    la prueba no dependa de que haya un LLM configurado ni de que ese
    modelo concreto acierte con la herramienta.
    """
    from app import motor

    for frase in ["¿por que ha cambiado la cifra?",
                  "por qué ha variado el importe",
                  "¿esto incluye datos corregidos?"]:
        detectada = motor._detectar_intencion(motor._normalizar(frase))
        assert detectada == "historial_correcciones", frase

    # Y la herramienta existe y responde por la ruta HTTP
    r = cliente.post(
        "/chat", headers=token_de(cliente, LUIS),
        json={"mensaje": "¿por que ha cambiado la cifra?", "session_id": str(uuid.uuid4())},
    )
    assert r.status_code == 200
    assert r.json()["respuesta"]


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


# =====================================================================
# Capa de proveedor de LLM
# =====================================================================

def test_el_catalogo_de_herramientas_depende_del_rol(cliente):
    """
    E7: un usuario normal no debe ver siquiera las herramientas que no
    puede usar. Es mas robusto que confiar en que el modelo se niegue.
    """
    from app.auth import Sesion
    from app import herramientas

    usuario = herramientas.herramientas_para(Sesion("a@x.es", "taxis_norte", "usuario"))
    operador = herramientas.herramientas_para(Sesion("b@x.es", "taxis_norte", "operador"))
    auditor = herramientas.herramientas_para(Sesion("c@x.es", "plataforma", "auditor"))

    nombres = lambda cat: {h["name"] for h in cat}
    assert "registrar_correccion" not in nombres(usuario)
    assert "registrar_correccion" in nombres(operador)
    assert "metricas_globales" not in nombres(operador)
    assert "metricas_globales" in nombres(auditor)
    # Ninguna herramienta expone un parametro de empresa al modelo
    for h in auditor:
        assert "empresa" not in str(h["input_schema"]).lower()


def test_traduccion_del_catalogo_al_formato_openai(cliente):
    """El mismo catalogo debe servir para Ollama, Groq y OpenAI."""
    from app.auth import Sesion
    from app import herramientas

    canonico = herramientas.herramientas_para(Sesion("b@x.es", "taxis_norte", "operador"))
    traducido = herramientas.a_formato_openai(canonico)

    assert len(traducido) == len(canonico)
    for original, convertido in zip(canonico, traducido):
        assert convertido["type"] == "function"
        assert convertido["function"]["name"] == original["name"]
        assert convertido["function"]["parameters"] == original["input_schema"]


def test_si_el_llm_falla_la_plataforma_responde_igual(cliente, monkeypatch):
    """
    Si el proveedor de LLM se cae (sin credito, contenedor parado, red),
    la peticion se resuelve en modo reglas en vez de devolver un error.
    """
    from app import config, motor

    def caerse(*args, **kwargs):
        raise ConnectionError("proveedor no disponible")

    monkeypatch.setattr(config, "LLM_ACTIVO", True)
    monkeypatch.setattr(config, "PROVEEDOR", "ollama")
    monkeypatch.setattr(motor, "_motor_llm", caerse)

    r = cliente.post(
        "/chat", headers=token_de(cliente, ANA),
        json={"mensaje": "cuantos viajes tenemos", "session_id": str(uuid.uuid4())},
    )
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["modo"] == "reglas"
    assert "ollama" in cuerpo["degradado_desde"]
    assert "400" in cuerpo["respuesta"] or "viajes" in cuerpo["respuesta"]
