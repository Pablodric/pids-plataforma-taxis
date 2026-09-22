"""
API de la plataforma.

Expone tres bloques:
  /auth/*        login y datos de la sesion
  /chat          el agente conversacional
  /datos/*       metricas y correcciones en JSON (alimentan el panel web)

Todas las rutas de datos y de chat pasan por:
  1. Validacion del JWT           -> identidad de empresa y rol (E7)
  2. Limitador de cuota por empresa (E7)
  3. Conexion a Postgres con contexto RLS (E7)
y registran el acceso en la auditoria, que alimenta las metricas de calidad.
"""

import json
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config, cuotas, db, motor, repositorio
from app.auth import Sesion, autenticar, emitir_token, exigir_rol, sesion_actual


@asynccontextmanager
async def ciclo_vida(app: FastAPI):
    db.abrir_recursos()
    yield
    db.cerrar_recursos()


app = FastAPI(
    title="Plataforma de datos de taxis - E6 + E7",
    description="Datos corregibles (E6) sobre una plataforma multiempresa (E7)",
    version="2.0",
    lifespan=ciclo_vida,
)

# El panel web se sirve como fichero estatico desde otro origen.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Cuota-Restante"],
)


# ---------------------------------------------------------------------
# Modelos de entrada
# ---------------------------------------------------------------------

class PeticionLogin(BaseModel):
    email: str
    password: str


class PeticionChat(BaseModel):
    mensaje: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=1, max_length=100)


class PeticionCorreccion(BaseModel):
    viaje_id: int
    campo: str
    valor_nuevo: float
    motivo: str = Field(min_length=3, max_length=500)


class PeticionCancelacion(BaseModel):
    viaje_id: int
    motivo: str = Field(min_length=3, max_length=500)


# ---------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------

def _con_cuota(sesion: Sesion, accion: str, coste: int = 1) -> dict:
    """Aplica la cuota de la empresa y deja rastro en la auditoria."""
    try:
        estado = cuotas.consumir(sesion.empresa_id, coste)
    except HTTPException:
        repositorio.registrar_auditoria(
            sesion.email, sesion.empresa_id, "cuota_superada", accion, False
        )
        raise
    return estado


def _historial(session_id: str, sesion: Sesion) -> list:
    clave = f"hist:{sesion.empresa_id}:{sesion.email}:{session_id}"
    crudo = db.cache().get(clave)
    return json.loads(crudo) if crudo else []


def _guardar_historial(session_id: str, sesion: Sesion, historial: list) -> None:
    clave = f"hist:{sesion.empresa_id}:{sesion.email}:{session_id}"
    recortado = historial[-config.MAX_TURNOS_HISTORIAL * 2:]
    db.cache().set(
        clave,
        json.dumps(recortado, ensure_ascii=False, default=str),
        ex=config.TTL_SESION_SEG,
    )


def _fecha(valor: str | None) -> date | None:
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Fecha no valida, usa YYYY-MM-DD")


# ---------------------------------------------------------------------
# Salud y metadatos
# ---------------------------------------------------------------------

@app.get("/salud", tags=["sistema"])
def salud():
    estado = db.comprobar_salud()
    todo_ok = all(estado.values())
    return JSONResponse(
        status_code=200 if todo_ok else 503,
        content={
            "estado": "ok" if todo_ok else "degradado",
            "componentes": estado,
            "modo_chatbot": config.descripcion_modo(),
        },
    )


@app.get("/metricas/calidad", tags=["sistema"])
def metricas_de_calidad():
    """Las tres metricas de calidad definidas para las restricciones E6 y E7."""
    return repositorio.metricas_calidad()


# ---------------------------------------------------------------------
# Autenticacion
# ---------------------------------------------------------------------

@app.post("/auth/login", tags=["auth"])
def login(peticion: PeticionLogin, request: Request):
    sesion = autenticar(peticion.email, peticion.password)
    if sesion is None:
        repositorio.registrar_auditoria(
            peticion.email, None, "login_fallido",
            f"ip={request.client.host if request.client else '?'}", False,
        )
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")

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
        "rol": sesion.rol,
        "puede_corregir": sesion.puede("operador"),
        "puede_ver_global": sesion.puede("auditor"),
        "cuota_consultas_min": cuotas.cuota_de(sesion.empresa_id),
    }


# ---------------------------------------------------------------------
# Chatbot
# ---------------------------------------------------------------------

@app.post("/chat", tags=["chatbot"])
def chat(peticion: PeticionChat, sesion: Sesion = Depends(sesion_actual)):
    estado_cuota = _con_cuota(sesion, "chat")
    historial = _historial(peticion.session_id, sesion)

    resultado = motor.responder(peticion.mensaje, historial, sesion)
    _guardar_historial(peticion.session_id, sesion, resultado.pop("historial"))

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


# ---------------------------------------------------------------------
# Datos (panel web y consumo directo)
# ---------------------------------------------------------------------

@app.get("/datos/resumen", tags=["datos"])
def datos_resumen(
    fecha: str | None = None,
    borough: str | None = None,
    sesion: Sesion = Depends(sesion_actual),
):
    _con_cuota(sesion, "datos_resumen")
    return repositorio.resumen_metricas(sesion, _fecha(fecha), borough)


@app.get("/datos/boroughs", tags=["datos"])
def datos_boroughs(sesion: Sesion = Depends(sesion_actual)):
    _con_cuota(sesion, "datos_boroughs")
    return repositorio.metricas_por_borough(sesion)


@app.get("/datos/zonas", tags=["datos"])
def datos_zonas(limite: int = 8, sesion: Sesion = Depends(sesion_actual)):
    _con_cuota(sesion, "datos_zonas")
    return repositorio.metricas_por_zona(sesion, min(limite, 25))


@app.get("/datos/horas", tags=["datos"])
def datos_horas(sesion: Sesion = Depends(sesion_actual)):
    _con_cuota(sesion, "datos_horas")
    return repositorio.viajes_por_hora(sesion)


@app.get("/datos/pagos", tags=["datos"])
def datos_pagos(sesion: Sesion = Depends(sesion_actual)):
    _con_cuota(sesion, "datos_pagos")
    return repositorio.reparto_pagos(sesion)


@app.get("/datos/viajes", tags=["datos"])
def datos_viajes(limite: int = 10, sesion: Sesion = Depends(sesion_actual)):
    _con_cuota(sesion, "datos_viajes")
    return repositorio.listar_viajes(sesion, min(limite, 50))


@app.get("/datos/globales", tags=["datos"])
def datos_globales(sesion: Sesion = Depends(exigir_rol("auditor"))):
    """E7: metricas entre empresas, solo para el rol autorizado."""
    _con_cuota(sesion, "datos_globales")
    return repositorio.metricas_globales(sesion)


# ---------------------------------------------------------------------
# E6: correcciones
# ---------------------------------------------------------------------

@app.get("/correcciones", tags=["correcciones"])
def listar_correcciones(
    viaje_id: int | None = None,
    limite: int = 20,
    sesion: Sesion = Depends(sesion_actual),
):
    _con_cuota(sesion, "listar_correcciones")
    return repositorio.historial_correcciones(sesion, viaje_id, min(limite, 100))


@app.post("/correcciones", tags=["correcciones"])
def crear_correccion(
    peticion: PeticionCorreccion,
    sesion: Sesion = Depends(exigir_rol("operador")),
):
    _con_cuota(sesion, "crear_correccion", coste=2)
    inicio = datetime.now()
    resultado = repositorio.registrar_correccion(
        sesion, peticion.viaje_id, peticion.campo,
        peticion.valor_nuevo, peticion.motivo,
    )
    if "error" in resultado:
        repositorio.registrar_auditoria(
            sesion.email, sesion.empresa_id, "correccion_rechazada",
            f"viaje={peticion.viaje_id}", False,
        )
        raise HTTPException(status_code=404, detail=resultado["error"])

    repositorio.registrar_auditoria(
        sesion.email, sesion.empresa_id, "correccion",
        f"viaje={peticion.viaje_id} campo={peticion.campo}", True,
        int((datetime.now() - inicio).total_seconds() * 1000),
    )
    return resultado


@app.post("/correcciones/cancelar", tags=["correcciones"])
def cancelar(
    peticion: PeticionCancelacion,
    sesion: Sesion = Depends(exigir_rol("operador")),
):
    _con_cuota(sesion, "cancelar_viaje", coste=2)
    resultado = repositorio.cancelar_viaje(sesion, peticion.viaje_id, peticion.motivo)
    if "error" in resultado:
        raise HTTPException(status_code=404, detail=resultado["error"])
    repositorio.registrar_auditoria(
        sesion.email, sesion.empresa_id, "cancelacion",
        f"viaje={peticion.viaje_id}", True,
    )
    return resultado
