"""
Herramientas que el modelo puede invocar (function calling).

Dos decisiones de diseno importantes:

  E7. 'empresa_id' no aparece en ningun esquema de los que ve el modelo.
      Se inyecta aqui, en el servidor, desde la sesion autenticada. El
      modelo no puede pedir datos de otra empresa porque no existe ningun
      parametro que se lo permita.

  E7. El catalogo de herramientas se construye SEGUN EL ROL. Si el usuario
      no es operador, las funciones de correccion ni siquiera se le
      ofrecen al modelo; y si no es auditor, tampoco las globales. Es mas
      robusto que confiar en que el modelo se niegue por si solo.
"""

from datetime import date, datetime

from app import repositorio
from app.auth import Sesion

# ---------------------------------------------------------------------
# Esquemas por rol
# ---------------------------------------------------------------------

HERRAMIENTAS_BASE = [
    {
        "name": "resumen_metricas",
        "description": (
            "Resumen de los viajes de la empresa del usuario: numero de viajes, "
            "importe medio, importe total, propina media y distancia media "
            "(importes en dolares USD, distancias en millas). Permite filtrar "
            "por fecha (YYYY-MM-DD) y por distrito de Nueva York."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD, opcional"},
                "borough": {
                    "type": "string",
                    "description": "Distrito: Manhattan, Brooklyn, Queens, Bronx o Staten Island",
                },
            },
        },
    },
    {
        "name": "metricas_por_zona",
        "description": "Zonas de recogida con mas viajes, con su importe medio.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limite": {"type": "integer", "description": "Cuantas zonas devolver (por defecto 8)"},
                "fecha": {"type": "string", "description": "Fecha YYYY-MM-DD, opcional"},
                "borough": {"type": "string", "description": "Distrito, opcional"},
            },
        },
    },
    {
        "name": "metricas_por_borough",
        "description": "Reparto de viajes e ingresos por distrito de Nueva York.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "viajes_por_hora",
        "description": "Distribucion de la demanda por hora del dia.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reparto_pagos",
        "description": "Reparto de viajes por metodo de pago y propina media de cada uno.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "listar_viajes",
        "description": (
            "Lista los ultimos viajes con su identificador, importe actual e "
            "importe original. Util para localizar un viaje que corregir."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limite": {"type": "integer", "description": "Cuantos viajes (por defecto 10)"}
            },
        },
    },
    {
        "name": "detalle_viaje",
        "description": (
            "Ficha de un viaje concreto: zona de origen y destino, valores tal y "
            "como se ingirieron, valores vigentes tras las correcciones, si esta "
            "cancelado y la cadena completa de cambios."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"viaje_id": {"type": "integer"}},
            "required": ["viaje_id"],
        },
    },
    {
        "name": "historial_correcciones",
        "description": (
            "Explica por que ha cambiado una cifra: devuelve las correcciones y "
            "cancelaciones registradas, con valor original, valor nuevo, motivo, "
            "autor y momento en que se aplicaron. Usala siempre que el usuario "
            "pregunte por que una metrica ha variado o si incluye datos corregidos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "viaje_id": {"type": "integer", "description": "Filtrar por un viaje concreto"},
                "limite": {"type": "integer", "description": "Maximo de registros (por defecto 20)"},
            },
        },
    },
]

HERRAMIENTAS_OPERADOR = [
    {
        "name": "registrar_correccion",
        "description": (
            "Registra que un proveedor ha corregido un dato de un viaje ya ingerido. "
            "Conserva el valor original y recalcula solo las metricas afectadas. "
            "Campos corregibles: importe_total, distancia, pasajeros, propina. "
            "Es una escritura permanente: antes de llamarla, confirma con el "
            "usuario el viaje, el campo y el valor exactos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "viaje_id": {"type": "integer"},
                "campo": {
                    "type": "string",
                    "enum": list(repositorio.CAMPOS_CORREGIBLES),
                },
                "valor_nuevo": {"type": "number"},
                "motivo": {"type": "string", "description": "Por que se corrige"},
            },
            "required": ["viaje_id", "campo", "valor_nuevo", "motivo"],
        },
    },
    {
        "name": "cancelar_viaje",
        "description": (
            "Cancela un viaje ya ingerido. Deja de contar en las metricas pero "
            "permanece en el historico con su motivo de cancelacion. Es "
            "irreversible: confirma antes con el usuario."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "viaje_id": {"type": "integer"},
                "motivo": {"type": "string"},
            },
            "required": ["viaje_id", "motivo"],
        },
    },
]

HERRAMIENTAS_AUDITOR = [
    {
        "name": "metricas_globales",
        "description": (
            "Metricas agregadas de TODAS las empresas de la plataforma. "
            "Solo disponible para el rol auditor."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def herramientas_para(sesion: Sesion) -> list[dict]:
    """Catalogo recortado al rol: el modelo no ve lo que no puede usar."""
    catalogo = list(HERRAMIENTAS_BASE)
    if sesion.puede("corregir"):
        catalogo += HERRAMIENTAS_OPERADOR
    if sesion.puede("ver_global"):
        catalogo += HERRAMIENTAS_AUDITOR
    return catalogo


ESCRITURAS = frozenset({"registrar_correccion", "cancelar_viaje"})


# ---------------------------------------------------------------------
# Ejecucion
# ---------------------------------------------------------------------

def _fecha(valor) -> date | None:
    if not valor:
        return None
    try:
        return datetime.strptime(str(valor)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _entero(valor, defecto: int) -> int:
    try:
        return int(valor)
    except (TypeError, ValueError):
        return defecto


def ejecutar(nombre: str, argumentos: dict, sesion: Sesion) -> dict:
    """
    Ejecuta la herramienta pedida por el modelo.

    El control de permisos se repite aqui aunque el catalogo ya este
    recortado: si el modelo alucinara un nombre de funcion que no le
    corresponde, la llamada se rechaza igualmente. Los argumentos mal
    formados devuelven un error legible en vez de una excepcion, para
    que el modelo pueda corregirse en la siguiente vuelta.
    """
    argumentos = argumentos or {}
    try:
        return _ejecutar(nombre, argumentos, sesion)
    except (KeyError, TypeError, ValueError) as exc:
        return {"error": f"Argumentos no validos para {nombre}: {exc}", "codigo": 422}


def _ejecutar(nombre: str, argumentos: dict, sesion: Sesion) -> dict:
    if nombre == "resumen_metricas":
        return repositorio.resumen_metricas(
            sesion, _fecha(argumentos.get("fecha")), argumentos.get("borough")
        )
    if nombre == "metricas_por_zona":
        return repositorio.metricas_por_zona(
            sesion, _entero(argumentos.get("limite"), 8),
            _fecha(argumentos.get("fecha")), argumentos.get("borough"),
        )
    if nombre == "metricas_por_borough":
        return repositorio.metricas_por_borough(sesion)
    if nombre == "viajes_por_hora":
        return repositorio.viajes_por_hora(sesion)
    if nombre == "reparto_pagos":
        return repositorio.reparto_pagos(sesion)
    if nombre == "listar_viajes":
        return repositorio.listar_viajes(sesion, _entero(argumentos.get("limite"), 10))
    if nombre == "detalle_viaje":
        return repositorio.detalle_viaje(sesion, int(argumentos["viaje_id"]))
    if nombre == "historial_correcciones":
        viaje = argumentos.get("viaje_id")
        return repositorio.historial_correcciones(
            sesion, int(viaje) if viaje is not None else None,
            _entero(argumentos.get("limite"), 20),
        )

    if nombre in ESCRITURAS:
        if not sesion.puede("corregir"):
            return {"error": "No autorizado: se requiere rol operador", "codigo": 403}
        if nombre == "registrar_correccion":
            return repositorio.registrar_correccion(
                sesion,
                int(argumentos["viaje_id"]),
                str(argumentos["campo"]),
                float(argumentos["valor_nuevo"]),
                str(argumentos.get("motivo") or "Corrección solicitada por chat"),
            )
        return repositorio.cancelar_viaje(
            sesion,
            int(argumentos["viaje_id"]),
            str(argumentos.get("motivo") or "Cancelación solicitada por chat"),
        )

    if nombre == "metricas_globales":
        if not sesion.puede("ver_global"):
            return {"error": "No autorizado: se requiere rol auditor", "codigo": 403}
        return repositorio.metricas_globales(sesion)

    return {"error": f"Herramienta desconocida: {nombre}", "codigo": 404}
