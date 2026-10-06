"""
Tiempo real: avisos de "viaje nuevo" hacia el panel por WebSocket.

Flujo
  1. POST /ingesta/viajes guarda el viaje (el disparador de la BD ya ha
     recalculado su cubo) y publica un aviso en Redis, canal viajes:<empresa>.
  2. Un unico hilo por proceso escucha viajes:* y reparte cada aviso a los
     WebSockets abiertos de esa empresa. Funciona con varias replicas de la
     API porque el bus es Redis, no la memoria del proceso.
  3. El panel, al recibir el aviso, vuelve a pedir /datos/panel. Por el
     WebSocket NO viaja ningun dato de negocio: los datos siguen saliendo por
     los endpoints de siempre, con su permiso, su cuota y su RLS (E7).

Autenticacion del WebSocket
  Un navegador no puede poner cabeceras en un WebSocket y el JWT no debe ir
  en la URL (queda en logs de nginx). Por eso: POST /ws/ticket (autenticado
  con el JWT de siempre) devuelve un ticket de un solo uso y 30 s de vida,
  y es el ticket lo que va en la URL. La empresa y el rol salen del ticket,
  que lo escribio el servidor: el cliente no elige a que canal se suscribe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time

from fastapi import WebSocket
from prometheus_client import Gauge

from app import db
from app.auth import PERMISOS_POR_ROL

log = logging.getLogger("pids.tiempo_real")

PATRON = "viajes:*"
TTL_TICKET_SEG = 30
LATIDO_SEG = 20          # ping periodico: mantiene viva la conexion tras nginx
GLOBAL = "*"             # ambito del auditor: recibe los avisos de todas las empresas

WS_CONEXIONES = Gauge("pids_ws_conexiones", "WebSockets del panel abiertos", ["ambito"])


def _texto(valor) -> str:
    return valor.decode() if isinstance(valor, bytes) else str(valor)


# ---------------------------------------------------------------------
# Tickets de un solo uso
# ---------------------------------------------------------------------

def emitir_ticket(sesion) -> str:
    ticket = secrets.token_urlsafe(24)
    db.cache().set(
        f"wsticket:{ticket}",
        json.dumps({"email": sesion.email, "empresa": sesion.empresa_id, "rol": sesion.rol}),
        ex=TTL_TICKET_SEG,
    )
    return ticket


def canjear_ticket(ticket: str) -> dict | None:
    """Lee y borra el ticket de forma atomica (MULTI): no se puede usar dos veces."""
    if not ticket or len(ticket) > 100:
        return None
    tuberia = db.cache().pipeline()
    clave = f"wsticket:{ticket}"
    tuberia.get(clave)
    tuberia.delete(clave)
    crudo, _ = tuberia.execute()
    if not crudo:
        return None
    try:
        return json.loads(_texto(crudo))
    except ValueError:
        return None


# ---------------------------------------------------------------------
# Publicacion (lado de la ingesta)
# ---------------------------------------------------------------------

def publicar(empresa_id: str, viaje_id: int, ingerido_en: str) -> None:
    """Avisa de un viaje ya confirmado. Un fallo aqui NUNCA debe tumbar la ingesta."""
    try:
        db.cache().publish(
            f"viajes:{empresa_id}",
            json.dumps({"tipo": "viaje", "viaje_id": viaje_id, "ts": ingerido_en}),
        )
    except Exception:  # noqa: BLE001
        log.warning("no se pudo publicar el aviso del viaje %s", viaje_id, exc_info=True)


# ---------------------------------------------------------------------
# Reparto (lado del WebSocket)
# ---------------------------------------------------------------------

class Difusor:
    def __init__(self) -> None:
        self._suscriptores: dict[str, set[asyncio.Queue]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hilo: threading.Thread | None = None
        self._parar = threading.Event()
        self._listo = threading.Event()

    # -- ciclo de vida -------------------------------------------------
    def asegurar(self, loop: asyncio.AbstractEventLoop) -> None:
        """Arranca el hilo escuchador la primera vez que hace falta."""
        self._loop = loop
        if self._hilo is None or not self._hilo.is_alive():
            self._parar.clear()
            self._listo.clear()
            self._hilo = threading.Thread(target=self._escuchar, name="difusor-viajes", daemon=True)
            self._hilo.start()

    def esperar_listo(self, segundos: float = 3.0) -> bool:
        return self._listo.wait(segundos)

    def parar(self) -> None:
        self._parar.set()

    # -- suscripciones -------------------------------------------------
    def alta(self, ambito: str) -> asyncio.Queue:
        # Solo es una senal de "hay novedades": si el cliente va lento se descartan avisos.
        cola: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._suscriptores.setdefault(ambito, set()).add(cola)
        WS_CONEXIONES.labels(ambito).inc()
        return cola

    def baja(self, ambito: str, cola: asyncio.Queue) -> None:
        conjunto = self._suscriptores.get(ambito)
        if conjunto and cola in conjunto:
            conjunto.discard(cola)
            WS_CONEXIONES.labels(ambito).dec()

    # -- hilo que escucha Redis ----------------------------------------
    def _escuchar(self) -> None:
        while not self._parar.is_set():
            try:
                bus = db.cache().pubsub(ignore_subscribe_messages=True)
                bus.psubscribe(PATRON)
                self._listo.set()
                while not self._parar.is_set():
                    msj = bus.get_message(timeout=1.0)
                    if msj and msj.get("type") == "pmessage":
                        loop = self._loop
                        if loop is not None and not loop.is_closed():
                            loop.call_soon_threadsafe(
                                self._repartir, _texto(msj["channel"]), _texto(msj["data"]))
                bus.close()
            except Exception:  # noqa: BLE001  (Redis caido: reintenta)
                self._listo.clear()
                log.warning("difusor: se perdio la conexion con Redis, reintento", exc_info=True)
                time.sleep(2)

    def _repartir(self, canal: str, datos: str) -> None:
        empresa = canal.split(":", 1)[1] if ":" in canal else canal
        destinos = set(self._suscriptores.get(empresa, ())) | set(self._suscriptores.get(GLOBAL, ()))
        for cola in destinos:
            try:
                cola.put_nowait(datos)
            except asyncio.QueueFull:
                pass


difusor = Difusor()


async def atender(ws: WebSocket, datos: dict) -> None:
    """Mantiene abierto un WebSocket ya autenticado hasta que se cierre."""
    permisos = PERMISOS_POR_ROL.get(datos.get("rol"), frozenset())
    if "consultar" not in permisos:
        await ws.close(code=4403)
        return
    ambito = GLOBAL if "ver_global" in permisos else datos["empresa"]

    difusor.asegurar(asyncio.get_running_loop())
    await asyncio.to_thread(difusor.esperar_listo)
    await ws.accept()
    cola = difusor.alta(ambito)
    recibir = asyncio.ensure_future(ws.receive_text())
    try:
        await ws.send_text(json.dumps({"tipo": "conectado", "ambito": ambito}))
        while True:
            sacar = asyncio.ensure_future(cola.get())
            hechas, _ = await asyncio.wait({recibir, sacar}, timeout=LATIDO_SEG,
                                           return_when=asyncio.FIRST_COMPLETED)
            if recibir in hechas:
                sacar.cancel()
                if recibir.exception() is not None:   # el cliente cerro
                    break
                recibir = asyncio.ensure_future(ws.receive_text())   # ignora lo que envie
                continue
            if sacar in hechas:
                await ws.send_text(sacar.result())
            else:
                sacar.cancel()
                await ws.send_text('{"tipo":"ping"}')
    except Exception:  # noqa: BLE001  (desconexion en mitad de un envio)
        pass
    finally:
        recibir.cancel()
        difusor.baja(ambito, cola)
