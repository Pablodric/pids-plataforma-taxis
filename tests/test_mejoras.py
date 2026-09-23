"""
Pruebas de regresion de los fallos corregidos en la version 3 y de las
funciones nuevas. Cada bloque dice que fallo cubre.

Ejecucion:  pytest -v tests/
"""

import json
import uuid
from datetime import date

import psycopg
import pytest
from app import config, db, motor, nlu
from conftest import ANA, AUDITOR, LUIS, MARTA, PABLO, token_de, viaje_disponible


def chat(cliente, cabecera, mensaje, sesion_chat):
    r = cliente.post("/chat", headers=cabecera,
                     json={"mensaje": mensaje, "session_id": sesion_chat})
    assert r.status_code == 200, r.text
    return r.json()


def total_correcciones(cliente, cabecera, viaje_id):
    return cliente.get(f"/correcciones?viaje_id={viaje_id}", headers=cabecera).json()["total"]


# =====================================================================
# E7. El auditor es de solo lectura
# Fallo v2: la jerarquia lineal de roles daba al auditor el permiso de
# escritura del operador, y podia corregir viajes de cualquier empresa.
# =====================================================================

def test_el_auditor_no_puede_corregir_ni_cancelar(cliente):
    viaje = viaje_disponible(cliente, token_de(cliente, LUIS))
    auditor = token_de(cliente, AUDITOR)

    r = cliente.post("/correcciones", headers=auditor,
                     json={"viaje_id": viaje["id"], "campo": "importe_total",
                           "valor_nuevo": 999.0, "motivo": "el auditor intenta escribir"})
    assert r.status_code == 403
    r = cliente.post("/correcciones/cancelar", headers=auditor,
                     json={"viaje_id": viaje["id"], "motivo": "el auditor intenta cancelar"})
    assert r.status_code == 403

    perfil = cliente.get("/auth/yo", headers=auditor).json()
    assert perfil["puede_corregir"] is False and perfil["puede_ver_global"] is True


def test_la_bd_impide_corregir_un_viaje_ajeno_aunque_falle_la_api(cliente):
    """Tercera barrera: aunque la API tuviera un fallo, la FK compuesta
    (viaje_id, empresa_id) impide anotar una correccion de una empresa
    sobre el viaje de otra."""
    ajeno = viaje_disponible(cliente, token_de(cliente, PABLO))["id"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation), \
            db.conexion_empresa("taxis_norte", "operador") as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO correcciones (viaje_id, empresa_id, tipo, campo,
                   valor_original, valor_nuevo, motivo, aplicada_por)
               VALUES (%s, 'taxis_norte', 'correccion', 'importe_total',
                       1, 2, 'ataque directo', 'test')""",
            (ajeno,),
        )


def test_la_bd_impide_escribir_al_auditor(cliente):
    """Aunque se salte la API, la politica RLS de insercion exige rol operador."""
    propio = viaje_disponible(cliente, token_de(cliente, LUIS))["id"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege), \
            db.conexion_empresa("taxis_norte", "auditor") as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO correcciones (viaje_id, empresa_id, tipo, campo,
                   valor_original, valor_nuevo, motivo, aplicada_por)
               VALUES (%s, 'taxis_norte', 'correccion', 'importe_total',
                       1, 2, 'auditor por SQL', 'test')""",
            (propio,),
        )


def test_el_historial_es_de_solo_anadir(cliente):
    """E6: el rol de la aplicacion no puede editar ni borrar correcciones."""
    for sql in ("UPDATE correcciones SET motivo = 'manipulado'",
                "DELETE FROM correcciones",
                "UPDATE viajes SET importe_total = 0"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), \
                db.conexion_empresa("taxis_norte", "operador") as conn, conn.cursor() as cur:
            cur.execute(sql)


# =====================================================================
# E6. Validacion y codigos de error
# =====================================================================

def test_no_se_puede_cancelar_dos_veces(cliente):
    cabecera = token_de(cliente, PABLO)
    viaje = viaje_disponible(cliente, cabecera)
    peticion = {"viaje_id": viaje["id"], "motivo": "servicio anulado"}
    assert cliente.post("/correcciones/cancelar", headers=cabecera, json=peticion).status_code == 200
    r = cliente.post("/correcciones/cancelar", headers=cabecera, json=peticion)
    assert r.status_code == 409


def test_un_viaje_cancelado_no_admite_correcciones(cliente):
    cabecera = token_de(cliente, PABLO)
    viaje = viaje_disponible(cliente, cabecera)
    cliente.post("/correcciones/cancelar", headers=cabecera,
                 json={"viaje_id": viaje["id"], "motivo": "servicio anulado"})
    r = cliente.post("/correcciones", headers=cabecera,
                     json={"viaje_id": viaje["id"], "campo": "propina",
                           "valor_nuevo": 1.0, "motivo": "tarde"})
    assert r.status_code == 409


@pytest.mark.parametrize("campo,valor", [
    ("importe_total", 150000.0),   # error de tecleo evidente
    ("distancia", -3.0),
    ("pasajeros", 2.5),            # los pasajeros son enteros
    ("pasajeros", 40.0),
])
def test_valores_imposibles_se_rechazan(cliente, campo, valor):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    r = cliente.post("/correcciones", headers=cabecera,
                     json={"viaje_id": viaje["id"], "campo": campo,
                           "valor_nuevo": valor, "motivo": "valor absurdo"})
    assert r.status_code == 422, r.text


def test_ficha_de_un_viaje_ajeno_es_404(cliente):
    ajeno = viaje_disponible(cliente, token_de(cliente, PABLO))["id"]
    assert cliente.get(f"/datos/viajes/{ajeno}", headers=token_de(cliente, LUIS)).status_code == 404
    propio = cliente.get(f"/datos/viajes/{ajeno}", headers=token_de(cliente, PABLO))
    assert propio.status_code == 200
    assert set(propio.json()["campos"]) == {"importe_total", "distancia", "pasajeros", "propina"}


def test_correccion_encadenada_conserva_cada_salto(cliente):
    """v0 -> v1 -> v2: cada correccion guarda el valor vigente al corregir."""
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    v0 = viaje["importe_total"]
    for nuevo in (v0 + 1, v0 + 2):
        assert cliente.post("/correcciones", headers=cabecera,
                            json={"viaje_id": viaje["id"], "campo": "importe_total",
                                  "valor_nuevo": round(nuevo, 2),
                                  "motivo": "ajuste"}).status_code == 200
    ficha = cliente.get(f"/datos/viajes/{viaje['id']}", headers=cabecera).json()
    saltos = [(h["valor_original"], h["valor_nuevo"]) for h in reversed(ficha["historial"])]
    assert saltos[-2:] == [(v0, round(v0 + 1, 2)), (round(v0 + 1, 2), round(v0 + 2, 2))]
    assert ficha["campos"]["importe_total"]["vigente"] == round(v0 + 2, 2)


# =====================================================================
# E7. Cuota: el panel no debe agotarla
# Fallo v2: cada refresco del panel gastaba 5 unidades y un operador se
# quedaba bloqueado tras 3 correcciones.
# =====================================================================

def test_el_panel_cuesta_una_unidad_de_cuota(cliente):
    cabecera = token_de(cliente, LUIS)
    r1 = cliente.get("/datos/panel", headers=cabecera)
    r2 = cliente.get("/datos/panel", headers=cabecera)
    assert r1.status_code == r2.status_code == 200
    restante1 = int(r1.headers["X-Cuota-Restante"])
    restante2 = int(r2.headers["X-Cuota-Restante"])
    assert restante1 - restante2 == 1

    datos = r1.json()
    assert {"resumen", "boroughs", "pagos", "zonas", "correcciones", "viajes"} <= set(datos)
    assert "globales" not in datos


def test_el_panel_del_auditor_incluye_globales_y_no_viajes_editables(cliente):
    datos = cliente.get("/datos/panel", headers=token_de(cliente, AUDITOR)).json()
    assert "globales" in datos and "viajes" not in datos


def test_un_operador_puede_trabajar_seguido_sin_bloquearse(cliente):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    for i in range(5):  # 5 x (correccion 2 + panel 1) = 15 < 20
        assert cliente.post("/correcciones", headers=cabecera,
                            json={"viaje_id": viaje["id"], "campo": "propina",
                                  "valor_nuevo": float(i + 1),
                                  "motivo": "ajuste en lote"}).status_code == 200
        assert cliente.get("/datos/panel", headers=cabecera).status_code == 200


def test_la_cuota_es_atomica_con_peticiones_simultaneas(cliente):
    """Con 40 peticiones concurrentes, nunca se conceden mas de las permitidas."""
    from concurrent.futures import ThreadPoolExecutor

    from app import cuotas

    limite = cuotas.cuota_de("movilidad_sur")

    def intento(_):
        try:
            cuotas.consumir("movilidad_sur")
            return True
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=16) as ex:
        concedidas = sum(ex.map(intento, range(40)))
    assert concedidas == limite


# =====================================================================
# Chatbot en modo reglas
# =====================================================================

def test_el_operador_corrige_por_chat_con_confirmacion(cliente):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    antes = total_correcciones(cliente, cabecera, viaje["id"])
    sesion_chat = str(uuid.uuid4())

    r = chat(cliente, cabecera,
             f"el viaje {viaje['id']} tenía mal la tarifa, eran 23,50", sesion_chat)
    assert r["pendiente_confirmacion"] is True
    assert "23,50" in r["respuesta"]
    assert total_correcciones(cliente, cabecera, viaje["id"]) == antes, \
        "No debe escribir nada hasta que el usuario confirme"

    r = chat(cliente, cabecera, "sí", sesion_chat)
    assert [u["herramienta"] for u in r["herramientas_usadas"]] == ["registrar_correccion"]
    assert total_correcciones(cliente, cabecera, viaje["id"]) == antes + 1
    ficha = cliente.get(f"/datos/viajes/{viaje['id']}", headers=cabecera).json()
    assert ficha["campos"]["importe_total"]["vigente"] == 23.5


def test_si_el_operador_no_confirma_no_se_escribe(cliente):
    cabecera = token_de(cliente, PABLO)
    viaje = viaje_disponible(cliente, cabecera)
    sesion_chat = str(uuid.uuid4())
    r = chat(cliente, cabecera,
             f"cancela el viaje {viaje['id']}, el cliente anuló el servicio", sesion_chat)
    assert r["pendiente_confirmacion"] is True
    assert "el cliente anuló el servicio" in r["respuesta"]
    r = chat(cliente, cabecera, "no", sesion_chat)
    assert r["herramientas_usadas"] == []
    ficha = cliente.get(f"/datos/viajes/{viaje['id']}", headers=cabecera).json()
    assert ficha["cancelado"] is False


def test_un_usuario_no_puede_corregir_por_chat(cliente):
    cabecera = token_de(cliente, ANA)
    viaje = viaje_disponible(cliente, cabecera)
    r = chat(cliente, cabecera, f"corrige el viaje {viaje['id']}, importe 10", str(uuid.uuid4()))
    assert r["pendiente_confirmacion"] is False
    assert r["herramientas_usadas"] == []
    assert "operador" in r["respuesta"]


def test_la_palabra_plataforma_no_se_confunde_con_otra_empresa(cliente):
    """Fallo v2: cualquier frase con 'plataforma' respondia 'No tengo acceso
    a los datos de Operador de la plataforma'."""
    r = chat(cliente, token_de(cliente, ANA),
             "¿cuántos viajes tenemos en la plataforma de taxis?", str(uuid.uuid4()))
    assert "Operador de la plataforma" not in r["respuesta"]


def test_nombrar_otra_empresa_se_deniega_explicitamente(cliente):
    r = chat(cliente, token_de(cliente, MARTA), "¿cuánto factura Taxis del Norte?",
             str(uuid.uuid4()))
    assert "No tengo acceso" in r["respuesta"]
    assert r["herramientas_usadas"] == []


def test_fechas_en_lenguaje_natural(cliente):
    r = chat(cliente, token_de(cliente, ANA), "¿cuánto facturamos el 1 de enero?",
             str(uuid.uuid4()))
    uso = r["herramientas_usadas"][0]
    assert uso["herramienta"] == "resumen_metricas"
    assert uso["argumentos"]["fecha"] == "2020-01-01"


def test_preguntas_de_seguimiento_usan_el_contexto(cliente):
    cabecera = token_de(cliente, ANA)
    sesion_chat = str(uuid.uuid4())
    chat(cliente, cabecera, "zonas con más viajes", sesion_chat)
    r = chat(cliente, cabecera, "¿y en Brooklyn?", sesion_chat)
    uso = r["herramientas_usadas"][0]
    assert uso["herramienta"] == "metricas_por_zona"
    assert uso["argumentos"]["borough"] == "Brooklyn"
    assert all(z["borough"] == "Brooklyn" for z in uso["resultado"]["zonas"])


def test_saludo_y_mensajes_no_entendidos_no_devuelven_metricas(cliente):
    cabecera = token_de(cliente, ANA)
    for mensaje in ("hola", "asdf qwerty"):
        r = chat(cliente, cabecera, mensaje, str(uuid.uuid4()))
        assert r["herramientas_usadas"] == []
        assert "Prueba, por ejemplo" in r["respuesta"]


def test_importes_en_dolares(cliente):
    r = chat(cliente, token_de(cliente, ANA), "¿cuántos viajes tenemos?", str(uuid.uuid4()))
    assert "$" in r["respuesta"] and "EUR" not in r["respuesta"]


# =====================================================================
# NLU (sin base de datos)
# =====================================================================

@pytest.mark.parametrize("mensaje,intencion", [
    ("¿Cuántos viajes tenemos?", "resumen_metricas"),
    ("Tarifa media en Manhattan", "resumen_metricas"),
    ("¿cuántos viajes hay ahora?", "resumen_metricas"),     # 'ahora' no es 'hora'
    ("¿A qué hora hay más demanda?", "viajes_por_hora"),
    ("Zonas con más viajes", "metricas_por_zona"),
    ("reparto por distrito", "metricas_por_borough"),
    ("¿cómo pagan nuestros clientes?", "reparto_pagos"),
    ("¿Por qué ha cambiado esa cifra?", "historial_correcciones"),
    ("¿esto incluye datos corregidos?", "historial_correcciones"),
    ("compara las dos empresas", "metricas_globales"),
    ("detalle del viaje 412", "detalle_viaje"),
    ("¿por qué ha cambiado el viaje 412?", "detalle_viaje"),
    ("el viaje 412 tenía mal la tarifa, eran 23,50", "registrar_correccion"),
    ("cambia la propina del viaje 7 a 5", "registrar_correccion"),
    ("cancela el viaje 88, el cliente anuló el servicio", "cancelar_viaje"),
    ("viajes 1 de enero", "resumen_metricas"),               # '1' no es un id de viaje
    ("hola", "saludo"),
    ("¿qué puedes hacer?", "ayuda"),
])
def test_nlu_intenciones(mensaje, intencion):
    assert nlu.interpretar(mensaje).intencion == intencion


def test_nlu_entidades_de_una_correccion():
    it = nlu.interpretar("El viaje nº 412 tenía mal la tarifa, eran 23,50 porque el taxímetro falló")
    assert (it.viaje_id, it.campo, it.valor) == (412, "importe_total", 23.5)
    assert it.motivo == "el taxímetro falló"


def test_nlu_fechas():
    disponibles = [date(2019, 12, 31), date(2020, 1, 1)]
    for texto, esperada in [("el 1 de enero", date(2020, 1, 1)),
                            ("en nochevieja", date(2019, 12, 31)),
                            ("el 31/12/2019", date(2019, 12, 31)),
                            ("2020-01-01", date(2020, 1, 1))]:
        partes, _ = nlu.extraer_fecha(nlu.normalizar(texto))
        assert nlu.resolver_fecha(partes, disponibles) == esperada, texto


# =====================================================================
# Chatbot en modo LLM (con un cliente simulado)
# Fallo v2: el historial guardaba bloques del SDK convertidos a texto con
# str(); en el segundo mensaje la API los rechazaba y el chat caia
# siempre a modo reglas.
# =====================================================================

class _Bloque:
    def __init__(self, **campos):
        self.__dict__.update(campos)

    def model_dump(self, exclude_none=True):
        return {k: v for k, v in self.__dict__.items() if v is not None}


class _Respuesta:
    def __init__(self, stop_reason, content):
        self.stop_reason, self.content = stop_reason, content


class _LLMSimulado:
    """Devuelve un guion de respuestas y guarda lo que recibe."""

    def __init__(self, guion):
        self.guion = list(guion)
        self.recibido = []
        self.messages = self

    def create(self, **kwargs):
        self.recibido.append(json.loads(json.dumps(kwargs["messages"], default=str)))
        siguiente = self.guion.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return siguiente


def _texto(t):
    return _Respuesta("end_turn", [_Bloque(type="text", text=t)])


def _herramienta(nombre, entrada):
    return _Respuesta("tool_use", [_Bloque(type="tool_use", id=f"tu_{uuid.uuid4().hex[:8]}",
                                            name=nombre, input=entrada)])


def test_modo_llm_guarda_solo_texto_en_el_historial(cliente, monkeypatch):
    llm = _LLMSimulado([
        _herramienta("resumen_metricas", {}), _texto("Tenéis 400 viajes."),
        _texto("De nada."),
    ])
    monkeypatch.setattr(config, "LLM_ACTIVO", True)
    monkeypatch.setattr(motor, "_cliente", lambda: llm)
    cabecera = token_de(cliente, ANA)
    sesion_chat = str(uuid.uuid4())

    r1 = chat(cliente, cabecera, "¿cuántos viajes tenemos?", sesion_chat)
    assert r1["modo"] == "llm" and r1["degradado_desde_llm"] is None
    r2 = chat(cliente, cabecera, "gracias", sesion_chat)
    assert r2["modo"] == "llm" and r2["respuesta"] == "De nada."

    # El segundo turno llega al modelo con un historial limpio de texto
    historial_turno2 = llm.recibido[-1]
    assert [m["role"] for m in historial_turno2] == ["user", "assistant", "user"]
    assert all(isinstance(m["content"], str) for m in historial_turno2)

    guardado = json.loads(db.cache().get(f"hist:taxis_norte:{ANA}:{sesion_chat}"))
    assert all(isinstance(m["content"], str) for m in guardado["historial"])


def test_si_el_llm_falla_tras_escribir_no_se_duplica_la_correccion(cliente, monkeypatch):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    antes = total_correcciones(cliente, cabecera, viaje["id"])
    llm = _LLMSimulado([
        _herramienta("registrar_correccion", {"viaje_id": viaje["id"], "campo": "propina",
                                              "valor_nuevo": 4.0, "motivo": "confirmado"}),
        RuntimeError("se corta la red"),
    ])
    monkeypatch.setattr(config, "LLM_ACTIVO", True)
    monkeypatch.setattr(motor, "_cliente", lambda: llm)

    r = chat(cliente, cabecera,
             f"sí, corrige la propina del viaje {viaje['id']} a 4", str(uuid.uuid4()))
    assert r["degradado_desde_llm"] == "RuntimeError"
    assert total_correcciones(cliente, cabecera, viaje["id"]) == antes + 1
    assert "Hecho" in r["respuesta"]


def test_si_el_llm_falla_sin_escribir_responde_el_modo_reglas(cliente, monkeypatch):
    llm = _LLMSimulado([RuntimeError("sin crédito")])
    monkeypatch.setattr(config, "LLM_ACTIVO", True)
    monkeypatch.setattr(motor, "_cliente", lambda: llm)
    r = chat(cliente, token_de(cliente, ANA), "¿cuántos viajes tenemos?", str(uuid.uuid4()))
    assert r["modo"] == "reglas" and r["degradado_desde_llm"] == "RuntimeError"
    assert r["herramientas_usadas"][0]["herramienta"] == "resumen_metricas"
