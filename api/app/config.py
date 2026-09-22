"""Configuracion leida del entorno, con valores por defecto para desarrollo."""

import os


def _entero(nombre: str, defecto: int) -> int:
    try:
        return int(os.environ.get(nombre, defecto))
    except ValueError:
        return defecto


# Conexion con el rol de aplicacion (NO superusuario): es lo que hace
# que las politicas RLS se apliquen de verdad.
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://pids_app:pids_app_pw@db:5432/pids"
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://cache:6379/0")

JWT_SECRETO = os.environ.get("JWT_SECRETO", "cambiar-en-produccion")
JWT_ALGORITMO = "HS256"
JWT_MINUTOS = _entero("JWT_MINUTOS", 480)


# ---------------------------------------------------------------------
# Motor del chatbot.
#
# La plataforma no esta atada a ningun proveedor de LLM. Hay tres
# opciones y se eligen con una variable de entorno:
#
#   reglas     NLU propio por palabras clave. Cero dependencias externas,
#              cero coste. Es el modo por defecto si no se configura nada.
#   ollama     Modelo abierto ejecutandose en un contenedor propio. Sin
#              clave, sin coste y sin que los datos salgan de la
#              plataforma. Habla el protocolo de OpenAI.
#   openai     Cualquier API compatible con OpenAI (Groq, Gemini, OpenAI).
#              Necesita clave, pero varias tienen nivel gratuito.
#   anthropic  API de Anthropic. Necesita clave de pago.
#
# Las herramientas y la capa de datos son las mismas en los cuatro casos:
# el aislamiento por empresa y las correcciones no dependen del motor.
# ---------------------------------------------------------------------

LLM_PROVEEDOR = os.environ.get("LLM_PROVEEDOR", "auto").strip().lower()
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODELO = os.environ.get("LLM_MODELO", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").strip()

# Valores por defecto de cada proveedor
_DEFECTOS = {
    "ollama": {"modelo": "qwen2.5:3b", "base_url": "http://ollama:11434/v1"},
    "openai": {"modelo": "llama-3.3-70b-versatile", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"modelo": "claude-sonnet-5", "base_url": ""},
}


def _resolver_proveedor() -> str:
    """
    'auto' elige el primer motor que este realmente configurado, para que
    el proyecto arranque siempre sin tocar nada.
    """
    if LLM_PROVEEDOR in ("reglas", "ollama", "openai", "anthropic"):
        return LLM_PROVEEDOR
    if LLM_BASE_URL:
        return "openai"
    if LLM_API_KEY:
        return "anthropic"
    return "reglas"


PROVEEDOR = _resolver_proveedor()

if PROVEEDOR != "reglas":
    _d = _DEFECTOS[PROVEEDOR]
    LLM_MODELO = LLM_MODELO or _d["modelo"]
    LLM_BASE_URL = LLM_BASE_URL or _d["base_url"]

# Ollama no pide clave real, pero el cliente de OpenAI exige que haya algo.
if PROVEEDOR == "ollama" and not LLM_API_KEY:
    LLM_API_KEY = "ollama"

LLM_ACTIVO = PROVEEDOR != "reglas"
LLM_TIMEOUT = _entero("LLM_TIMEOUT", 120)

# E7: ventana del limitador de consumo por empresa.
VENTANA_CUOTA_SEG = _entero("VENTANA_CUOTA_SEG", 60)

# Historial de conversacion
MAX_TURNOS_HISTORIAL = _entero("MAX_TURNOS_HISTORIAL", 12)
TTL_SESION_SEG = _entero("TTL_SESION_SEG", 3600)


def descripcion_modo() -> str:
    """Texto corto para /salud y para la cabecera del panel."""
    if PROVEEDOR == "reglas":
        return "reglas"
    return f"{PROVEEDOR}:{LLM_MODELO}"
