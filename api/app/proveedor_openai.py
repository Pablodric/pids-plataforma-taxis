"""
Bucle de function calling contra cualquier API compatible con OpenAI.

Con este unico modulo funcionan:
  - Ollama            modelo abierto en un contenedor propio, sin clave
  - Groq              nivel gratuito, muy rapido
  - Google Gemini     a traves de su endpoint compatible con OpenAI
  - OpenAI            el original

Lo unico que cambia entre ellos es 'LLM_BASE_URL' y 'LLM_MODELO'. Esa es
la razon de separar el motor en proveedores: cambiar de uno a otro es
editar el .env, no tocar codigo.
"""

import json

from app import config, herramientas
from app.auth import Sesion

MAX_ITERACIONES = 6


def _argumentos(bruto) -> dict:
    """
    Los modelos pequenos a veces devuelven los argumentos como cadena JSON
    y a veces ya como objeto; y de vez en cuando con JSON mal formado.
    """
    if isinstance(bruto, dict):
        return bruto
    if not bruto:
        return {}
    try:
        cargado = json.loads(bruto)
        return cargado if isinstance(cargado, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def responder(mensaje: str, historial: list, sesion: Sesion, instrucciones: str):
    from openai import OpenAI

    cliente = OpenAI(
        api_key=config.LLM_API_KEY or "sin-clave",
        base_url=config.LLM_BASE_URL,
        timeout=config.LLM_TIMEOUT,
    )

    # En el protocolo de OpenAI las instrucciones van como primer mensaje
    # de rol 'system', no en un parametro aparte.
    mensajes = [{"role": "system", "content": instrucciones}]
    mensajes += historial
    mensajes.append({"role": "user", "content": mensaje})

    catalogo = herramientas.a_formato_openai(herramientas.herramientas_para(sesion))
    usadas: list[dict] = []

    for _ in range(MAX_ITERACIONES):
        respuesta = cliente.chat.completions.create(
            model=config.LLM_MODELO,
            max_tokens=1200,
            messages=mensajes,
            tools=catalogo,
        )
        salida = respuesta.choices[0].message
        llamadas = salida.tool_calls or []

        if not llamadas:
            texto = salida.content or ""
            mensajes.append({"role": "assistant", "content": texto})
            # El historial que se guarda no incluye el mensaje de sistema:
            # se vuelve a anteponer en cada turno desde la configuracion.
            return texto, mensajes[1:], usadas

        mensajes.append({
            "role": "assistant",
            "content": salida.content or "",
            "tool_calls": [
                {
                    "id": lc.id,
                    "type": "function",
                    "function": {"name": lc.function.name,
                                 "arguments": lc.function.arguments or "{}"},
                }
                for lc in llamadas
            ],
        })

        for lc in llamadas:
            argumentos = _argumentos(lc.function.arguments)
            resultado = herramientas.ejecutar(lc.function.name, argumentos, sesion)
            usadas.append({"herramienta": lc.function.name,
                           "argumentos": argumentos, "resultado": resultado})
            mensajes.append({
                "role": "tool",
                "tool_call_id": lc.id,
                "content": json.dumps(resultado, ensure_ascii=False, default=str),
            })

    return ("No he podido completar la consulta en un numero razonable de pasos.",
            mensajes[1:], usadas)
