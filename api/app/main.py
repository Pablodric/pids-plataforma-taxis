"""
API de la plataforma.

Expone cuatro bloques:
  /auth/*        login y datos de la sesion
  /chat          el agente conversacional
  /datos/*       metricas en JSON (alimentan el panel web)
  /correcciones  E6: registrar y consultar correcciones
  /ingesta/*     viajes nuevos en tiempo real (cuentas de proveedor)
  /ws/*          avisos de viaje nuevo hacia el panel (WebSocket)

Todas las rutas de datos y de chat pasan por:
  1. Validacion del JWT y del permiso      -> empresa y rol (E7)
  2. Limitador de cuota por empresa        (E7)
  3. Conexion a Postgres con contexto RLS  (E7)
y registran el acceso en la auditoria, que alimenta las metricas de calidad.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config, cuotas, db, motor, nlu_llm, repositorio, tiempo_real
from app import observabilidad as obs
from app.auth import Sesion, autenticar, emitir_token, exigir_permiso, sesion_actual


@asynccontextmanager
async def ciclo_vida(app: FastAPI):
    db.abrir_recursos()
    if config.OLLAMA_ACTIVO:
        # Carga el modelo en segundo plano: la API arranca sin esperarle
        import threading
        threading.Thread(target=nlu_llm.cliente().calentar, daemon=True).start()
    yield
    tiempo_real.difusor.parar()
    db.cerrar_recursos()


app = FastAPI(
    title="Plataforma de datos de taxis - E6 + E7",
    description="Datos corregibles (E6) sobre una plataforma multiempresa (E7)",
    version="3.0",
    lifespan=ciclo_vida,
)

# El panel se sirve normalmente a traves del proxy de nginx (mismo origen,
# ruta /api). CORS queda abierto para poder abrir index.html suelto o usar
# la API desde otras herramientas durante la demo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Cuota-Limite", "X-Cuota-Restante", "Retry-After", "X-Request-ID"],
)
app.add_middleware(obs.MiddlewareObservabilidad)
obs.configurar_logs()


# ---------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------

class PeticionLogin(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class PeticionChat(BaseModel):
    mensaje: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=1, max_length=100)


class PeticionCorreccion(BaseModel):
    viaje_id: int = Field(gt=0)
    campo: Literal["importe_total", "distancia", "pasajeros", "propina"]
    valor_nuevo: float
    motivo: str = Field(min_length=3, max_length=500)


class PeticionViaje(BaseModel):
    """Un viaje nuevo, con los mismos nombres de columna que el CSV de la TLC.
    No lleva empresa: la pone el servidor a partir del token (E7)."""
    VendorID: int | None = None
    tpep_pickup_datetime: str = Field(min_length=8, max_length=40)
    tpep_dropoff_datetime: str | None = Field(default=None, max_length=40)
    passenger_count: int | None = Field(default=None, ge=0, le=9)
    trip_distance: float | None = Field(default=None, ge=0, le=500)
    PULocationID: int = Field(ge=1, le=265)
    DOLocationID: int | None = Field(default=None, ge=1, le=265)
    payment_type: int | None = None
    fare_amount: float | None = None
    tip_amount: float | None = Field(default=None, ge=0, le=1000)
    tolls_amount: float | None = None
    congestion_surcharge: float | None = None
    total_amount: float = Field(ge=-1000, le=10000)


class PeticionCancelacion(BaseModel):
    viaje_id: int = Field(gt=0)
    motivo: str = Field(min_length=3, max_length=500)


# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------

def _con_cuota(sesion: Sesion, accion: str, coste: int = 1) -> dict:
    """Aplica la cuota de la empresa y deja rastro en la auditoria."""
    try:
        return cuotas.consumir(sesion.empresa_id, coste)
    except HTTPException:
        obs.CUOTA_RECHAZOS.labels(sesion.empresa_id).inc()
        # Se audita UNA vez por ventana, no cada rechazo: con una rafaga de
        # miles de 429 cada uno escribia en Postgres, y el rechazo "barato"
        # acababa cargando la base de datos que comparten todas las empresas
        # (lo destapo scripts/vecino_ruidoso.py). El recuento exacto esta en
        # la metrica pids_cuota_rechazos_total.
        if db.cache().set(f"cuota:auditada:{sesion.empresa_id}", 1, nx=True,
                          ex=config.VENTANA_CUOTA_SEG):
            repositorio.registrar_auditoria(
                sesion.email, sesion.empresa_id, "cuota_superada", accion, False
            )
        raise


def cobrar(accion: str, coste: int = 1, permiso: str = "consultar"):
    """
    Dependencia: comprueba el permiso, consume cuota y publica el estado
    de la cuota en las cabeceras X-Cuota-*.
    Uso: sesion: Sesion = Depends(cobrar("datos_resumen"))
    """
    def dependencia(response: Response,
                    sesion: Sesion = Depends(exigir_permiso(permiso))) -> Sesion:
        estado = _con_cuota(sesion, accion, coste)
        response.headers["X-Cuota-Limite"] = str(estado["limite"])
        response.headers["X-Cuota-Restante"] = str(estado["restantes"])
        return sesion
    return dependencia


def _o_error(resultado: dict) -> dict:
    """Traduce un error de negocio del repositorio a su codigo HTTP."""
    if "error" in resultado:
        raise HTTPException(status_code=resultado.get("codigo", 400),
                            detail=resultado["error"])
    return resultado


def _clave_conversacion(session_id: str, sesion: Sesion) -> str:
    return f"hist:{sesion.empresa_id}:{sesion.email}:{session_id}"


def _cargar_conversacion(session_id: str, sesion: Sesion) -> tuple[list, dict]:
    crudo = db.cache().get(_clave_conversacion(session_id, sesion))
    if not crudo:
        return [], {}
    try:
        estado = json.loads(crudo)
    except ValueError:
        return [], {}
    if isinstance(estado, list):          # formato antiguo: solo historial
        return [m for m in estado if isinstance(m.get("content"), str)], {}
    return estado.get("historial", []), estado.get("contexto", {})


def _guardar_conversacion(session_id: str, sesion: Sesion,
                          historial: list, contexto: dict) -> None:
    recortado = historial[-config.MAX_TURNOS_HISTORIAL * 2:]
    db.cache().set(
        _clave_conversacion(session_id, sesion),
        json.dumps({"historial": recortado, "contexto": contexto},
                   ensure_ascii=False, default=str),
        ex=config.TTL_SESION_SEG,
    )


def _fecha(valor: str | None) -> date | None:
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Fecha no válida, usa YYYY-MM-DD") from None


# ---------------------------------------------------------------------
# Salud y metadatos
# ---------------------------------------------------------------------

@app.get("/salud", tags=["sistema"])
def salud():
    estado = db.comprobar_salud()
    todo_ok = all(estado.values())
    contenido = {
        "estado": "ok" if todo_ok else "degradado",
        "componentes": estado,
        "modo_chatbot": _modo_chatbot(),
        "modelo": config.LLM_MODELO if config.LLM_ACTIVO else None,
    }
    if config.OLLAMA_ACTIVO:
        # Ollama no cuenta para el estado de salud: si falta, el chatbot
        # sigue funcionando con reglas (degradado, no caido).
        contenido["ollama"] = nlu_llm.estado()
        contenido["modelo"] = contenido["ollama"]["modelo"]
    return JSONResponse(status_code=200 if todo_ok else 503, content=contenido)


def _modo_chatbot() -> str:
    if config.LLM_ACTIVO:
        return "llm"
    if config.OLLAMA_ACTIVO:
        disponible = nlu_llm.cliente().modelo_activo() is not None
        obs.OLLAMA_DISPONIBLE.set(1 if disponible else 0)
        return "ollama" if disponible else "reglas"
    return "reglas"


@app.get("/metrics", tags=["sistema"], include_in_schema=False)
def metricas_prometheus():
    """Métricas en formato Prometheus (latencias por ruta, cuota, NLU...)."""
    _modo_chatbot()
    return obs.exponer()


@app.get("/metricas/calidad", tags=["sistema"])
def metricas_de_calidad():
    """Las métricas de calidad definidas para las restricciones E6 y E7."""
    return repositorio.metricas_calidad()


# ---------------------------------------------------------------------
# Autenticacion
# ---------------------------------------------------------------------

# Freno a la fuerza bruta: tras MAX_FALLOS fallos seguidos para un correo
# (o muchos desde una misma IP) se bloquea el login un tiempo, incluso con
# la contrasena correcta. Contadores en Redis: validos con varias replicas.
MAX_FALLOS_CORREO, MAX_FALLOS_IP, BLOQUEO_SEG = 5, 30, 300


def _bloqueado(claves: list[tuple[str, int]]) -> int | None:
    for clave, maximo in claves:
        if int(db.cache().get(clave) or 0) >= maximo:
            return max(1, db.cache().ttl(clave))
    return None


@app.post("/auth/login", tags=["auth"])
def login(peticion: PeticionLogin, request: Request):
    ip = request.client.host if request.client else "?"
    correo = peticion.email.lower().strip()
    claves = [(f"login:correo:{correo}", MAX_FALLOS_CORREO), (f"login:ip:{ip}", MAX_FALLOS_IP)]
    espera = _bloqueado(claves)
    if espera:
        obs.LOGIN_BLOQUEOS.inc()
        repositorio.registrar_auditoria(correo, None, "login_bloqueado", f"ip={ip}", False)
        raise HTTPException(status_code=429, headers={"Retry-After": str(espera)},
                            detail=f"Demasiados intentos fallidos. Espera {espera} s.")

    sesion = autenticar(peticion.email, peticion.password)
    if sesion is None:
        obs.LOGIN_FALLIDOS.inc()
        tuberia = db.cache().pipeline()
        for clave, _ in claves:
            tuberia.incr(clave)
            tuberia.expire(clave, BLOQUEO_SEG)
        tuberia.execute()
        repositorio.registrar_auditoria(correo, None, "login_fallido", f"ip={ip}", False)
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    db.cache().delete(claves[0][0])

    token, expira_en = emitir_token(sesion)
    repositorio.registrar_auditoria(
        sesion.email, sesion.empresa_id, "login", f"rol={sesion.rol}", True
    )
    return {
        "token": token,
        "expira_en_seg": expira_en,
        "email": sesion.email,
        "empresa": sesion.empresa_id,
        "rol": sesion.rol,
    }


@app.get("/auth/yo", tags=["auth"])
def quien_soy(sesion: Sesion = Depends(sesion_actual)):
    return {
        "email": sesion.email,
        "empresa": sesion.empresa_id,
        "empresa_nombre": repositorio.nombre_empresa(sesion.empresa_id),
        "rol": sesion.rol,
        "puede_corregir": sesion.puede("corregir"),
        "puede_ver_global": sesion.puede("ver_global"),
        "cuota_consultas_min": cuotas.cuota_de(sesion.empresa_id),
        "modo_chatbot": _modo_chatbot(),
        "modelo_nlu": (nlu_llm.cliente().modelo_activo() if config.OLLAMA_ACTIVO else None),
        "nlu_afinado": (nlu_llm.cliente().es_afinado(nlu_llm.cliente().modelo_activo())
                        if config.OLLAMA_ACTIVO else False),
    }


# ---------------------------------------------------------------------
# Chatbot
# ---------------------------------------------------------------------

@app.post("/chat", tags=["chatbot"])
def chat(peticion: PeticionChat, sesion: Sesion = Depends(exigir_permiso("consultar"))):
    estado_cuota = _con_cuota(sesion, "chat")
    historial, contexto = _cargar_conversacion(peticion.session_id, sesion)

    resultado = motor.responder(peticion.mensaje, historial, sesion, contexto)
    obs.CHAT.labels(resultado["nlu"], resultado["modo"]).inc()
    if resultado["degradado_desde_llm"]:
        obs.NLU_FALLOS.labels(resultado["degradado_desde_llm"]).inc()
    for uso in resultado["herramientas_usadas"]:
        if uso["herramienta"] in ("registrar_correccion", "cancelar_viaje") \
                and "error" not in uso["resultado"]:
            obs.CORRECCIONES.labels(sesion.empresa_id, uso["herramienta"]).inc()
    _guardar_conversacion(peticion.session_id, sesion,
                          resultado.pop("historial"), resultado.pop("contexto"))

    repositorio.registrar_auditoria(
        sesion.email, sesion.empresa_id, "chat",
        peticion.mensaje[:200], True, resultado["latencia_ms"],
    )

    return {
        **resultado,
        "empresa": sesion.empresa_id,
        "rol": sesion.rol,
        "cuota": estado_cuota,
    }


@app.delete("/chat/{session_id}", tags=["chatbot"])
def olvidar_conversacion(session_id: str, sesion: Sesion = Depends(sesion_actual)):
    """Empieza una conversacion nueva (borra historial y contexto)."""
    db.cache().delete(_clave_conversacion(session_id, sesion))
    return {"ok": True}


# ---------------------------------------------------------------------
# Datos (panel web y consumo directo)
# ---------------------------------------------------------------------

@app.get("/datos/panel", tags=["datos"])
def datos_panel(sesion: Sesion = Depends(cobrar("datos_panel"))):
    """Todo lo que pinta el panel web, en una petición y una unidad de cuota."""
    datos = repositorio.panel(sesion)
    return datos


@app.get("/datos/resumen", tags=["datos"])
def datos_resumen(
    fecha: str | None = None,
    borough: str | None = None,
    sesion: Sesion = Depends(cobrar("datos_resumen")),
):
    return repositorio.resumen_metricas(sesion, _fecha(fecha), borough)


@app.get("/datos/boroughs", tags=["datos"])
def datos_boroughs(sesion: Sesion = Depends(cobrar("datos_boroughs"))):
    return repositorio.metricas_por_borough(sesion)


@app.get("/datos/zonas", tags=["datos"])
def datos_zonas(limite: int = 8, fecha: str | None = None, borough: str | None = None,
                sesion: Sesion = Depends(cobrar("datos_zonas"))):
    return repositorio.metricas_por_zona(sesion, limite, _fecha(fecha), borough)


@app.get("/datos/horas", tags=["datos"])
def datos_horas(sesion: Sesion = Depends(cobrar("datos_horas"))):
    return repositorio.viajes_por_hora(sesion)


@app.get("/datos/pagos", tags=["datos"])
def datos_pagos(sesion: Sesion = Depends(cobrar("datos_pagos"))):
    return repositorio.reparto_pagos(sesion)


@app.get("/datos/viajes", tags=["datos"])
def datos_viajes(limite: int = 10, sesion: Sesion = Depends(cobrar("datos_viajes"))):
    return repositorio.listar_viajes(sesion, limite)


@app.get("/datos/viajes/{viaje_id}", tags=["datos"])
def datos_viaje(viaje_id: int, sesion: Sesion = Depends(cobrar("datos_viaje"))):
    """Ficha de un viaje: valores originales, vigentes y cadena de cambios."""
    return _o_error(repositorio.detalle_viaje(sesion, viaje_id))


@app.get("/datos/en", tags=["datos"])
def datos_en_instante(instante: str | None = None, antes_de_correccion: int | None = None,
                      sesion: Sesion = Depends(cobrar("datos_en"))):
    """
    E6 · Viaje en el tiempo: métricas tal y como estaban en un instante
    pasado (ISO 8601), reconstruidas desde el log de correcciones, más las
    actuales y la diferencia. Con `antes_de_correccion=<id>` se usa el
    instante justo anterior a esa corrección (precisión de microsegundos).
    """
    from datetime import timedelta
    if antes_de_correccion is not None:
        momento = repositorio.momento_de_correccion(sesion, antes_de_correccion)
        if momento is None:
            raise HTTPException(status_code=404, detail="Esa corrección no existe en tu empresa")
        resultado = repositorio.resumen_en(sesion, momento - timedelta(microseconds=1))
        resultado["antes_de_correccion"] = antes_de_correccion
        return resultado
    if not instante:
        raise HTTPException(status_code=400, detail="Indica 'instante' o 'antes_de_correccion'")
    try:
        momento = datetime.fromisoformat(instante.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Instante no válido, usa ISO 8601") from None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    return repositorio.resumen_en(sesion, momento)


@app.get("/auditoria/cadena", tags=["auditoria"])
def auditoria_cadena(sesion: Sesion = Depends(cobrar("verificar_cadena"))):
    """
    Verifica la cadena SHA-256 de correcciones (la de tu empresa; todas si
    eres auditor). Cualquier modificación o borrado, incluso hecho por un
    superusuario de la base de datos, rompe la cadena y se señala aquí.
    """
    return repositorio.verificar_cadena(sesion)


@app.get("/datos/globales", tags=["datos"])
def datos_globales(
    sesion: Sesion = Depends(cobrar("datos_globales", permiso="ver_global")),
):
    """E7: métricas entre empresas, solo para el rol autorizado."""
    return repositorio.metricas_globales(sesion)


# ---------------------------------------------------------------------
# E6: correcciones
# ---------------------------------------------------------------------

@app.get("/correcciones", tags=["correcciones"])
def listar_correcciones(
    viaje_id: int | None = None,
    limite: int = 20,
    sesion: Sesion = Depends(cobrar("listar_correcciones")),
):
    return repositorio.historial_correcciones(sesion, viaje_id, limite)


@app.post("/correcciones", tags=["correcciones"])
def crear_correccion(
    peticion: PeticionCorreccion,
    sesion: Sesion = Depends(cobrar("crear_correccion", coste=2, permiso="corregir")),
):
    inicio = datetime.now()
    resultado = repositorio.registrar_correccion(
        sesion, peticion.viaje_id, peticion.campo,
        peticion.valor_nuevo, peticion.motivo,
    )
    if "error" in resultado:
        repositorio.registrar_auditoria(
            sesion.email, sesion.empresa_id, "correccion_rechazada",
            f"viaje={peticion.viaje_id}: {resultado['error']}",
            resultado.get("codigo") not in (403, 404),
        )
        return _o_error(resultado)

    obs.CORRECCIONES.labels(sesion.empresa_id, "registrar_correccion").inc()
    repositorio.registrar_auditoria(
        sesion.email, sesion.empresa_id, "correccion",
        f"viaje={peticion.viaje_id} campo={peticion.campo}", True,
        int((datetime.now() - inicio).total_seconds() * 1000),
    )
    return resultado


@app.post("/correcciones/cancelar", tags=["correcciones"])
def cancelar(
    peticion: PeticionCancelacion,
    sesion: Sesion = Depends(cobrar("cancelar_viaje", coste=2, permiso="corregir")),
):
    resultado = repositorio.cancelar_viaje(sesion, peticion.viaje_id, peticion.motivo)
    if "error" in resultado:
        repositorio.registrar_auditoria(
            sesion.email, sesion.empresa_id, "cancelacion_rechazada",
            f"viaje={peticion.viaje_id}: {resultado['error']}",
            resultado.get("codigo") not in (403, 404),
        )
        return _o_error(resultado)
    obs.CORRECCIONES.labels(sesion.empresa_id, "cancelar_viaje").inc()
    repositorio.registrar_auditoria(
        sesion.email, sesion.empresa_id, "cancelacion",
        f"viaje={peticion.viaje_id}", True,
    )
    return resultado


# ---------------------------------------------------------------------
# Ingesta en tiempo real
# ---------------------------------------------------------------------

FORMATOS_FECHA = ("%m/%d/%Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def _fecha_hora(valor: str | None, campo: str) -> datetime | None:
    if not valor or not valor.strip():
        return None
    for formato in FORMATOS_FECHA:
        try:
            return datetime.strptime(valor.strip(), formato)
        except ValueError:
            continue
    raise HTTPException(status_code=422, detail=f"Fecha no válida en {campo}: {valor!r}")


@app.post("/ingesta/viajes", status_code=201, tags=["ingesta"])
def ingerir_viaje(
    peticion: PeticionViaje,
    response: Response,
    sesion: Sesion = Depends(exigir_permiso("ingerir")),
):
    """
    Recibe un viaje recién terminado. La empresa propietaria es la de la
    credencial que lo envía (nunca un campo del viaje), y la ingesta tiene
    su propia cuota por empresa, separada de la de consultas.
    """
    try:
        estado = cuotas.consumir(sesion.empresa_id, clase="ingesta")
    except HTTPException:
        obs.VIAJES_INGERIDOS.labels(sesion.empresa_id, "cuota_superada").inc()
        raise
    response.headers["X-Cuota-Limite"] = str(estado["limite"])
    response.headers["X-Cuota-Restante"] = str(estado["restantes"])

    recogida = _fecha_hora(peticion.tpep_pickup_datetime, "tpep_pickup_datetime")
    llegada = _fecha_hora(peticion.tpep_dropoff_datetime, "tpep_dropoff_datetime")
    resultado = repositorio.registrar_viaje(sesion, peticion.model_dump(), recogida, llegada)
    if "error" in resultado:
        obs.VIAJES_INGERIDOS.labels(sesion.empresa_id, "rechazado").inc()
        return _o_error(resultado)
    obs.VIAJES_INGERIDOS.labels(sesion.empresa_id, "aceptado").inc()
    tiempo_real.publicar(sesion.empresa_id, resultado["viaje_id"], resultado["ingerido_en"])
    return resultado


# ---------------------------------------------------------------------
# Tiempo real hacia el panel (WebSocket)
# ---------------------------------------------------------------------

@app.post("/ws/ticket", tags=["tiempo real"])
def ws_ticket(sesion: Sesion = Depends(exigir_permiso("consultar"))):
    """Ticket de un solo uso (30 s) para abrir /ws/viajes sin poner el JWT en la URL."""
    return {"ticket": tiempo_real.emitir_ticket(sesion),
            "expira_en_seg": tiempo_real.TTL_TICKET_SEG}


@app.websocket("/ws/viajes")
async def ws_viajes(ws: WebSocket, ticket: str = ""):
    """
    Avisos de viaje nuevo de TU empresa (el auditor recibe los de todas). El
    ambito sale del ticket, que emitio el servidor: el cliente no elige canal.
    No viajan datos de negocio, solo la senal para refrescar el panel.
    """
    datos = await asyncio.to_thread(tiempo_real.canjear_ticket, ticket)
    if datos is None:
        await ws.close(code=4401)   # sin aceptar antes: el navegador ve un rechazo del handshake
        return
    await tiempo_real.atender(ws, datos)
