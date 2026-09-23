"""
Contrato del NLU: el JSON que produce el modelo de lenguaje (y, para poder
compararlos, tambien el NLU por reglas).

Es la UNICA fuente de verdad del formato. La usan:
  - la API (nlu_llm.py), para pedir a Ollama una salida con este esquema;
  - el generador del dataset y el entrenamiento (carpeta llm/), para que el
    modelo aprenda exactamente lo que despues se le pide;
  - la evaluacion, para comparar reglas, modelo base y modelo afinado.

Por que el modelo hace de NLU y no de agente completo: un modelo pequeno
(0,5-1,5B) que corre en CPU es poco fiable encadenando llamadas a
herramientas y redactando cifras, pero extrae intencion y entidades muy
bien tras un fine-tuning corto. Las cifras, los permisos y las
confirmaciones siguen saliendo del codigo y de la base de datos.
"""

import json
import re

INTENCIONES = [
    "resumen_metricas",       # cuantos viajes, facturacion, tarifa media...
    "metricas_por_zona",      # zonas / barrios con mas viajes
    "metricas_por_borough",   # reparto por distrito
    "viajes_por_hora",        # distribucion horaria, hora punta
    "reparto_pagos",          # metodos de pago, propinas por metodo
    "listar_viajes",          # ultimos viajes
    "detalle_viaje",          # ficha de un viaje concreto
    "historial_correcciones", # por que ha cambiado una cifra, correcciones
    "metricas_globales",      # comparar empresas / toda la plataforma
    "registrar_correccion",   # corregir un dato de un viaje (E6)
    "cancelar_viaje",         # cancelar un viaje (E6)
    "saludo",
    "ayuda",
    "gracias",
    "afirmar",                # si, confirmo
    "negar",                  # no, dejalo
    "seguimiento",            # "¿y en Brooklyn?": repite la consulta anterior
    "fuera_de_dominio",       # nada que ver con la plataforma
]

CAMPOS = ["importe_total", "distancia", "pasajeros", "propina"]
BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island", "EWR"]

# Fecha: "AAAA-MM-DD" si el usuario dice el anio, "--MM-DD" si no lo dice
# (ISO 8601 parcial). El anio que falta lo resuelve el servidor mirando
# que fechas hay en los datos.
RE_FECHA = re.compile(r"^(\d{4}|-)-(\d{2})-(\d{2})$")

ESQUEMA = {
    "type": "object",
    "properties": {
        "intencion": {"type": "string", "enum": INTENCIONES},
        "viaje_id": {"type": ["integer", "null"]},
        # Enumeraciones sin "type": Ollama convierte el esquema en una
        # gramatica (llama.cpp) y asi la alternativa null queda explicita.
        "campo": {"enum": CAMPOS + [None]},
        "valor": {"type": ["number", "null"]},
        "borough": {"enum": BOROUGHS + [None]},
        "fecha": {"type": ["string", "null"]},
        "motivo": {"type": ["string", "null"]},
    },
    "required": ["intencion", "viaje_id", "campo", "valor", "borough", "fecha", "motivo"],
}

CLAVES = list(ESQUEMA["properties"])

PROMPT_SISTEMA = """Eres el NLU de un chatbot de una plataforma de datos de taxis de Nueva York. \
Convierte el mensaje del usuario (en español) en un JSON con estas claves:
- intencion: una de """ + ", ".join(INTENCIONES) + """.
- viaje_id: número del viaje si se menciona (entero) o null.
- campo: dato de un viaje que se quiere corregir: importe_total (tarifa, precio, importe, total), \
propina, distancia (millas, recorrido) o pasajeros; null si no aplica.
- valor: valor nuevo de la corrección como número (coma decimal → punto) o null.
- borough: Manhattan, Brooklyn, Queens, Bronx, Staten Island o EWR si se menciona; si no, null.
- fecha: "AAAA-MM-DD" si dice el año, "--MM-DD" si no lo dice, o null.
- motivo: motivo literal de una corrección o cancelación si lo da, o null.
Usa "seguimiento" cuando el mensaje solo cambia un filtro de la pregunta anterior ("¿y en Brooklyn?"), \
"afirmar"/"negar" para confirmaciones, y "fuera_de_dominio" si no tiene que ver con la plataforma. \
Responde solo con el JSON."""


def vacio(intencion: str = "fuera_de_dominio") -> dict:
    return {"intencion": intencion, "viaje_id": None, "campo": None, "valor": None,
            "borough": None, "fecha": None, "motivo": None}


def serializar(marco: dict) -> str:
    """JSON compacto con las claves siempre en el mismo orden: es lo que se
    entrena como respuesta del modelo."""
    return json.dumps({k: marco.get(k) for k in CLAVES}, ensure_ascii=False)


def validar(marco: dict) -> dict:
    """
    Normaliza y valida la salida del modelo. Cualquier valor fuera del
    esquema se descarta (se pone a null) en vez de propagarse: la salida de
    un modelo es una entrada no confiable.
    """
    if not isinstance(marco, dict):
        raise ValueError("La salida del NLU no es un objeto JSON")
    limpio = vacio()
    intencion = marco.get("intencion")
    if intencion not in INTENCIONES:
        raise ValueError(f"Intención desconocida: {intencion!r}")
    limpio["intencion"] = intencion

    viaje = marco.get("viaje_id")
    if isinstance(viaje, str) and viaje.strip().isdigit():
        viaje = int(viaje)
    if isinstance(viaje, (int, float)) and not isinstance(viaje, bool) and viaje > 0 \
            and float(viaje).is_integer():
        limpio["viaje_id"] = int(viaje)

    if marco.get("campo") in CAMPOS:
        limpio["campo"] = marco["campo"]

    valor = marco.get("valor")
    if isinstance(valor, str):
        try:
            valor = float(valor.replace(",", ".").replace("$", "").strip())
        except ValueError:
            valor = None
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        limpio["valor"] = round(float(valor), 2)

    if marco.get("borough") in BOROUGHS:
        limpio["borough"] = marco["borough"]

    fecha = marco.get("fecha")
    if isinstance(fecha, str) and RE_FECHA.match(fecha.strip()):
        limpio["fecha"] = fecha.strip()

    motivo = marco.get("motivo")
    if isinstance(motivo, str) and len(motivo.strip()) >= 3:
        limpio["motivo"] = motivo.strip()[:300]
    return limpio


def fecha_a_partes(fecha: str | None):
    """'2020-01-01' -> (2020, 1, 1); '--12-31' -> (None, 12, 31)."""
    if not fecha:
        return None
    m = RE_FECHA.match(fecha)
    if not m:
        return None
    anio = None if m.group(1) == "-" else int(m.group(1))
    return (anio, int(m.group(2)), int(m.group(3)))


def partes_a_fecha(partes) -> str | None:
    if not partes:
        return None
    anio, mes, dia = partes
    return f"{anio:04d}-{mes:02d}-{dia:02d}" if anio else f"--{mes:02d}-{dia:02d}"
