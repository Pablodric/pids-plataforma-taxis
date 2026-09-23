"""
Observabilidad: metricas Prometheus, identificador de peticion y logs JSON.

  GET /metrics     formato Prometheus (lo puede leer Prometheus/Grafana tal cual)
  X-Request-ID     se respeta si llega (p. ej. de nginx) o se genera; vuelve en
                   la respuesta y en cada linea de log, para seguir una peticion
                   de extremo a extremo.
  Logs             una linea JSON por peticion en stdout (docker compose logs api)

La etiqueta de ruta es la PLANTILLA (/datos/viajes/{viaje_id}), no la URL
concreta: con ids en la etiqueta la cardinalidad de las series creceria sin
limite, que es el error clasico al instrumentar una API.
"""

import json
import logging
import sys
import time
import uuid

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

DURACION = Histogram(
    "pids_http_duracion_segundos", "Duración de las peticiones HTTP",
    ["metodo", "ruta", "estado"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
CHAT = Counter("pids_chat_mensajes_total", "Mensajes del chatbot por NLU y modo", ["nlu", "modo"])
NLU_FALLOS = Counter("pids_nlu_degradaciones_total",
                     "Mensajes en que el modelo falló y respondieron las reglas", ["motivo"])
CUOTA_RECHAZOS = Counter("pids_cuota_rechazos_total", "Peticiones rechazadas por cuota (429)",
                         ["empresa"])
CORRECCIONES = Counter("pids_correcciones_total", "Correcciones y cancelaciones registradas",
                       ["empresa", "tipo"])
LOGIN_FALLIDOS = Counter("pids_login_fallidos_total", "Intentos de login fallidos")
LOGIN_BLOQUEOS = Counter("pids_login_bloqueos_total", "Logins bloqueados por demasiados fallos")
OLLAMA_DISPONIBLE = Gauge("pids_ollama_disponible", "1 si el modelo de Ollama responde")

_log = logging.getLogger("pids")


class _FormatoJSON(logging.Formatter):
    def format(self, registro: logging.LogRecord) -> str:
        datos = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(registro.created)),
                 "nivel": registro.levelname.lower(), "mensaje": registro.getMessage()}
        datos.update(getattr(registro, "extra_json", {}))
        return json.dumps(datos, ensure_ascii=False)


def configurar_logs() -> None:
    if not _log.handlers:
        salida = logging.StreamHandler(sys.stdout)
        salida.setFormatter(_FormatoJSON())
        _log.addHandler(salida)
        _log.setLevel(logging.INFO)
        _log.propagate = False


class MiddlewareObservabilidad(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        inicio = time.perf_counter()
        estado = 500
        try:
            respuesta = await call_next(request)
            estado = respuesta.status_code
        finally:
            ruta = getattr(request.scope.get("route"), "path", "no_encontrada")
            segundos = time.perf_counter() - inicio
            if ruta != "/metrics":
                DURACION.labels(request.method, ruta, str(estado)).observe(segundos)
                _log.info("peticion", extra={"extra_json": {
                    "request_id": rid, "metodo": request.method, "ruta": ruta,
                    "estado": estado, "ms": round(segundos * 1000, 1)}})
        respuesta.headers["X-Request-ID"] = rid
        return respuesta


def exponer() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
