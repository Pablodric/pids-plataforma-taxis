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

JWT_SECRETO = os.environ.get("JWT_SECRETO", "").strip()
if len(JWT_SECRETO) < 32:
    # HS256 exige al menos 32 bytes. Si el .env no trae un secreto valido
    # se genera uno aleatorio al arrancar: la demo funciona igual y los
    # tokens simplemente caducan si se reinicia la API.
    import secrets as _secrets
    JWT_SECRETO = _secrets.token_urlsafe(48)
JWT_ALGORITMO = "HS256"
JWT_MINUTOS = _entero("JWT_MINUTOS", 480)

# ---------------------------------------------------------------------
# Motor del chatbot. LLM_PROVEEDOR:
#   "ollama"    -> modelo local afinado (carpeta llm/) como NLU. Opcion por
#                  defecto en docker compose: sin coste, sin datos fuera.
#   "anthropic" -> agente con function calling sobre la API de Anthropic.
#   "reglas"    -> solo el NLU por reglas.
# En cualquier modo, si el modelo no responde se usan las reglas.
# ---------------------------------------------------------------------
LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_MODELO = os.environ.get("LLM_MODELO", "claude-sonnet-5")
LLM_PROVEEDOR = os.environ.get(
    "LLM_PROVEEDOR", "anthropic" if LLM_API_KEY else "reglas"
).strip().lower()
LLM_ACTIVO = LLM_PROVEEDOR == "anthropic" and bool(LLM_API_KEY)
OLLAMA_ACTIVO = LLM_PROVEEDOR == "ollama"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Modelo afinado (lo crea llm/instalar_en_ollama.sh) y modelo base de
# respaldo, que se usa mientras no se haya instalado el afinado.
OLLAMA_MODELO = os.environ.get("OLLAMA_MODELO", "pids-nlu")
OLLAMA_MODELO_RESPALDO = os.environ.get("OLLAMA_MODELO_RESPALDO", "qwen2.5:1.5b")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "30"))

# E7: ventana del limitador de consumo por empresa.
VENTANA_CUOTA_SEG = _entero("VENTANA_CUOTA_SEG", 60)
# Cuota propia de la ingesta en tiempo real (viajes por ventana y empresa).
# Va aparte de la de consultas: asi el flujo de viajes de una empresa no deja
# sin cuota a sus usuarios, y sigue sin poder saturar a las demas.
CUOTA_INGESTA = _entero("CUOTA_INGESTA", 120)

# Historial de conversacion
MAX_TURNOS_HISTORIAL = _entero("MAX_TURNOS_HISTORIAL", 12)
TTL_SESION_SEG = _entero("TTL_SESION_SEG", 3600)
