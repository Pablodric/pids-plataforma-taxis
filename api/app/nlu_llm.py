"""
NLU con un modelo local servido por Ollama.

El modelo (Qwen2.5 afinado con LoRA, ver carpeta llm/) recibe el mensaje y
devuelve el JSON de esquema_nlu. Tres protecciones, porque la salida de un
modelo es una entrada no confiable:

  1. Salida estructurada: se envia el esquema JSON en 'format' y Ollama
     restringe la generacion con una gramatica. El modelo no puede devolver
     una intencion inexistente ni un JSON mal formado.
  2. Validacion: esquema_nlu.validar() descarta cualquier valor fuera del
     contrato.
  3. Anclaje al texto: el numero de viaje, el valor y el motivo tienen que
     aparecer en el mensaje. Si el modelo "se inventa" un viaje, se descarta
     y el motor pregunta en lugar de corregir un viaje equivocado.

Si Ollama no responde, o el modelo afinado no esta instalado y tampoco el
de respaldo, el motor usa el NLU por reglas: la plataforma nunca depende
del modelo para funcionar.
"""

import json
import logging
import re
import threading
import time
import unicodedata

import httpx

from app import config
from app import esquema_nlu as E

# Ejemplos para el modelo BASE (sin afinar). El modelo afinado no los
# necesita: ha aprendido el formato en el entrenamiento, y se le envia
# exactamente el mismo prompt que vio entonces.
EJEMPLOS_POCOS_DISPAROS = [
    ("¿cuántos viajes tuvimos en Manhattan el 1 de enero?",
     {"intencion": "resumen_metricas", "borough": "Manhattan", "fecha": "--01-01"}),
    ("el viaje 411 tenía mal la tarifa, eran 23,50",
     {"intencion": "registrar_correccion", "viaje_id": 411, "campo": "importe_total", "valor": 23.5}),
    ("cancela el viaje 88, el cliente anuló el servicio",
     {"intencion": "cancelar_viaje", "viaje_id": 88, "motivo": "el cliente anuló el servicio"}),
    ("¿y en Brooklyn?", {"intencion": "seguimiento", "borough": "Brooklyn"}),
    ("sí", {"intencion": "afirmar"}),
]

_PALABRAS_NUMERO = {"uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
                    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10}


def _sin_tildes(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.lower())
                   if unicodedata.category(c) != "Mn")


def anclar(marco: dict, texto: str) -> dict:
    """
    Descarta las entidades que no aparecen en el mensaje (alucinaciones).
    Es barato y evita el peor fallo posible: corregir un viaje que el
    usuario no ha mencionado.
    """
    marco = dict(marco)
    t = _sin_tildes(texto)
    enteros = set(re.findall(r"\d+", t))
    if marco.get("viaje_id") is not None and str(marco["viaje_id"]) not in enteros:
        marco["viaje_id"] = None
    if marco.get("valor") is not None:
        numeros = [float(n.replace(",", ".")) for n in re.findall(r"\d+(?:[.,]\d+)?", t)]
        numeros += [float(v) for p, v in _PALABRAS_NUMERO.items() if re.search(rf"\b{p}\b", t)]
        if not any(abs(n - marco["valor"]) < 0.005 for n in numeros):
            marco["valor"] = None
    if marco.get("motivo") and _sin_tildes(marco["motivo"]).strip(" .") not in t:
        marco["motivo"] = None
    return marco


class ClienteOllama:
    """Cliente minimo de la API HTTP de Ollama (/api/chat y /api/tags)."""

    def __init__(self, url: str, modelo: str, respaldo: str | None = None,
                 timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.modelo = modelo
        self.respaldo = respaldo
        self.timeout = timeout
        self._http = httpx.Client(timeout=timeout)
        self._cache_modelo: tuple[float, str | None] = (0.0, None)
        self._cerrojo = threading.Lock()

    # ---------- descubrimiento ----------
    def modelos_instalados(self) -> list[str]:
        r = self._http.get(f"{self.url}/api/tags", timeout=3)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]

    def modelo_activo(self, cache_seg: float = 30.0) -> str | None:
        """
        El modelo afinado si esta instalado; si no, el de respaldo (base);
        si Ollama no responde, None. Se cachea unos segundos para no
        preguntar a Ollama en cada mensaje.
        """
        with self._cerrojo:
            instante, modelo = self._cache_modelo
            if time.monotonic() - instante < cache_seg:
                return modelo
            try:
                instalados = self.modelos_instalados()
                modelo = None
                for candidato in (self.modelo, self.respaldo):
                    if candidato and any(n == candidato or n == f"{candidato}:latest"
                                         or n.split(":")[0] == candidato for n in instalados):
                        modelo = candidato
                        break
            except (httpx.HTTPError, ValueError, KeyError):
                modelo = None
            self._cache_modelo = (time.monotonic(), modelo)
            return modelo

    def es_afinado(self, modelo: str | None) -> bool:
        return modelo is not None and modelo == self.modelo

    # ---------- inferencia ----------
    def _mensajes(self, texto: str, afinado: bool) -> list[dict]:
        mensajes = [{"role": "system", "content": E.PROMPT_SISTEMA}]
        if not afinado:
            for ejemplo, marco in EJEMPLOS_POCOS_DISPAROS:
                completo = E.vacio(marco["intencion"])
                completo.update(marco)
                mensajes += [{"role": "user", "content": ejemplo},
                             {"role": "assistant", "content": E.serializar(completo)}]
        mensajes.append({"role": "user", "content": texto})
        return mensajes

    def interpretar(self, texto: str, modelo: str | None = None,
                    afinado: bool | None = None) -> dict:
        """Mensaje -> marco validado y anclado. Lanza excepcion si falla.
        'afinado' None: se deduce (el modelo afinado es self.modelo)."""
        modelo = modelo or self.modelo_activo() or self.modelo
        if afinado is None:
            afinado = self.es_afinado(modelo)
        r = self._http.post(f"{self.url}/api/chat", json={
            "model": modelo,
            "messages": self._mensajes(texto, afinado=afinado),
            "format": E.ESQUEMA,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 160, "num_ctx": 2048},
        })
        r.raise_for_status()
        contenido = r.json()["message"]["content"]
        marco = E.validar(json.loads(contenido))
        return anclar(marco, texto)

    def calentar(self) -> None:
        """Carga el modelo en memoria para que el primer mensaje no espere."""
        modelo = self.modelo_activo(cache_seg=0)
        if modelo:
            try:
                self.interpretar("hola", modelo)
            except Exception as exc:  # calentar es opcional: el primer mensaje esperara
                logging.getLogger("pids").warning("no se pudo precargar %s: %s", modelo, exc)


_cliente: ClienteOllama | None = None


def cliente() -> ClienteOllama:
    global _cliente
    if _cliente is None:
        _cliente = ClienteOllama(config.OLLAMA_URL, config.OLLAMA_MODELO,
                                 config.OLLAMA_MODELO_RESPALDO, config.OLLAMA_TIMEOUT)
    return _cliente


def estado() -> dict:
    """Para /salud: si Ollama responde y que modelo se esta usando."""
    c = cliente()
    modelo = c.modelo_activo()
    return {
        "url": c.url,
        "disponible": modelo is not None,
        "modelo": modelo,
        "afinado": c.es_afinado(modelo),
    }
