"""
Integracion con Ollama (NLU con modelo local afinado).

Se prueba contra un servidor que habla el mismo protocolo HTTP que Ollama
(tests/ollama_simulado.py): lo que se verifica aqui es la integracion
(peticion, esquema, validacion, anclaje, respaldo por reglas), no la
calidad del modelo, que se mide aparte con llm/evaluar.py.
"""

import json
import uuid

import pytest
from app import config, esquema_nlu, nlu_llm
from conftest import ANA, LUIS, token_de, viaje_disponible
from ollama_simulado import OllamaSimulado


@pytest.fixture
def ollama(monkeypatch):
    with OllamaSimulado() as sim:
        monkeypatch.setattr(config, "OLLAMA_ACTIVO", True)
        monkeypatch.setattr(config, "LLM_ACTIVO", False)
        monkeypatch.setattr(nlu_llm, "_cliente",
                            nlu_llm.ClienteOllama(sim.url, "pids-nlu", "qwen2.5:1.5b", timeout=5))
        yield sim


def chat(cliente, cabecera, mensaje, sesion_chat):
    r = cliente.post("/chat", headers=cabecera,
                     json={"mensaje": mensaje, "session_id": sesion_chat})
    assert r.status_code == 200, r.text
    return r.json()


def test_el_chat_usa_el_modelo_afinado_con_salida_estructurada(cliente, ollama):
    r = chat(cliente, token_de(cliente, ANA), "¿cuántos viajes tenemos?", str(uuid.uuid4()))
    assert r["modo"] == "ollama" and r["nlu"] == "ollama:pids-nlu"
    assert r["herramientas_usadas"][0]["herramienta"] == "resumen_metricas"

    peticion = ollama.peticiones[-1]
    assert peticion["model"] == "pids-nlu"
    assert peticion["format"] == json.loads(json.dumps(esquema_nlu.ESQUEMA))
    assert peticion["stream"] is False and peticion["options"]["temperature"] == 0
    # El modelo afinado recibe exactamente el prompt con el que se entreno
    assert [m["role"] for m in peticion["messages"]] == ["system", "user"]
    assert peticion["messages"][0]["content"] == esquema_nlu.PROMPT_SISTEMA


def test_sin_modelo_afinado_usa_el_base_con_ejemplos(cliente, ollama):
    ollama.modelos = ["qwen2.5:1.5b"]
    nlu_llm.cliente()._cache_modelo = (0.0, None)
    r = chat(cliente, token_de(cliente, ANA), "zonas con más viajes", str(uuid.uuid4()))
    assert r["nlu"] == "ollama:qwen2.5:1.5b"
    mensajes = ollama.peticiones[-1]["messages"]
    assert len(mensajes) == 2 + 2 * len(nlu_llm.EJEMPLOS_POCOS_DISPAROS)


def test_correccion_por_chat_con_ollama_y_confirmacion(cliente, ollama):
    cabecera = token_de(cliente, LUIS)
    viaje = viaje_disponible(cliente, cabecera)
    sesion_chat = str(uuid.uuid4())
    r = chat(cliente, cabecera, f"el viaje {viaje['id']} tenía mal la propina, eran 4,25",
             sesion_chat)
    assert r["nlu"] == "ollama:pids-nlu" and r["pendiente_confirmacion"] is True
    r = chat(cliente, cabecera, "sí", sesion_chat)
    assert r["herramientas_usadas"][0]["herramienta"] == "registrar_correccion"
    ficha = cliente.get(f"/datos/viajes/{viaje['id']}", headers=cabecera).json()
    assert ficha["campos"]["propina"]["vigente"] == 4.25


def test_un_viaje_inventado_por_el_modelo_se_descarta(cliente, ollama):
    """El modelo 'alucina' un viaje que el usuario no ha dicho: el anclaje lo
    descarta y el bot pregunta en vez de preparar la correccion."""
    ollama.respuesta_fija = esquema_nlu.serializar({
        **esquema_nlu.vacio("registrar_correccion"),
        "viaje_id": 777, "campo": "importe_total", "valor": 99.0})
    r = chat(cliente, token_de(cliente, LUIS), "corrige la tarifa a 99", str(uuid.uuid4()))
    assert r["pendiente_confirmacion"] is False
    assert "¿De qué viaje" in r["respuesta"]


def test_json_invalido_del_modelo_cae_a_reglas(cliente, ollama):
    ollama.respuesta_fija = "esto no es json"
    r = chat(cliente, token_de(cliente, ANA), "¿cuántos viajes tenemos?", str(uuid.uuid4()))
    assert r["modo"] == "reglas" and r["degradado_desde_llm"] == "JSONDecodeError"
    assert r["herramientas_usadas"][0]["herramienta"] == "resumen_metricas"


def test_intencion_fuera_del_esquema_cae_a_reglas(cliente, ollama):
    ollama.respuesta_fija = json.dumps({**esquema_nlu.vacio(), "intencion": "borrar_todo"})
    r = chat(cliente, token_de(cliente, ANA), "¿cuántos viajes tenemos?", str(uuid.uuid4()))
    assert r["modo"] == "reglas" and r["degradado_desde_llm"] == "ValueError"


def test_si_ollama_no_esta_el_chat_sigue_con_reglas(cliente, monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_ACTIVO", True)
    monkeypatch.setattr(nlu_llm, "_cliente",
                        nlu_llm.ClienteOllama("http://127.0.0.1:9", "pids-nlu", "qwen2.5:1.5b", 2))
    r = chat(cliente, token_de(cliente, ANA), "¿cuántos viajes tenemos?", str(uuid.uuid4()))
    assert r["modo"] == "reglas" and r["degradado_desde_llm"] == "OllamaNoDisponible"
    salud = cliente.get("/salud").json()
    assert salud["estado"] == "ok" and salud["ollama"]["disponible"] is False


def test_salud_informa_del_modelo(cliente, ollama):
    salud = cliente.get("/salud").json()
    assert salud["modo_chatbot"] == "ollama"
    assert salud["ollama"] == {"url": ollama.url, "disponible": True,
                               "modelo": "pids-nlu", "afinado": True}


@pytest.mark.parametrize("marco,texto,esperado", [
    ({"viaje_id": 12}, "detalle del viaje 12", {"viaje_id": 12}),
    ({"viaje_id": 13}, "detalle del viaje 12", {"viaje_id": None}),
    ({"valor": 23.5}, "eran 23,50", {"valor": 23.5}),
    ({"valor": 3.0}, "eran tres pasajeros", {"valor": 3.0}),
    ({"valor": 30.0}, "eran 23,50", {"valor": None}),
    ({"motivo": "el taxímetro falló"}, "porque el taximetro fallo", {"motivo": "el taxímetro falló"}),
    ({"motivo": "otra cosa"}, "porque el taximetro fallo", {"motivo": None}),
])
def test_anclaje_de_entidades(marco, texto, esperado):
    completo = {**esquema_nlu.vacio("registrar_correccion"), **marco}
    anclado = nlu_llm.anclar(completo, texto)
    for k, v in esperado.items():
        assert anclado[k] == v


def test_validacion_del_esquema():
    v = esquema_nlu.validar({"intencion": "registrar_correccion", "viaje_id": "12",
                             "campo": "tarifa", "valor": "23,5", "borough": "Madrid",
                             "fecha": "ayer", "motivo": "ok"})
    assert v == {**esquema_nlu.vacio("registrar_correccion"), "viaje_id": 12, "valor": 23.5}
    with pytest.raises(ValueError):
        esquema_nlu.validar({"intencion": "inventada"})


CARPETA_LLM = __import__("pathlib").Path(__file__).resolve().parents[1] / "llm"


@pytest.mark.skipif(not (CARPETA_LLM / "evaluar.py").exists(), reason="carpeta llm/ no montada")
def test_la_evaluacion_funciona_contra_ollama(ollama):
    """El script de evaluacion habla con Ollama igual que la API."""
    import sys
    sys.path.insert(0, str(CARPETA_LLM))
    import evaluar

    ejemplos = [{"texto": "detalle del viaje 5",
                 "esperado": {**esquema_nlu.vacio("detalle_viaje"), "viaje_id": 5}},
                {"texto": "hola", "esperado": esquema_nlu.vacio("saludo")}]
    r = evaluar.evaluar(evaluar.motor_ollama(ollama.url, "pids-nlu"), ejemplos)
    assert r["intencion_acierto"] == 1.0 and r["marco_exacto"] == 1.0
