"""
Bucle de function calling contra la API de Anthropic.

Se mantiene separado del proveedor compatible con OpenAI porque el
formato de mensajes y de llamadas a herramientas es distinto. La logica
de negocio no esta aqui: este modulo solo traduce entre el modelo y
'herramientas.ejecutar()'.
"""

import json

from app import config, herramientas
from app.auth import Sesion

MAX_ITERACIONES = 6


def responder(mensaje: str, historial: list, sesion: Sesion, instrucciones: str):
    from anthropic import Anthropic

    cliente = Anthropic(api_key=config.LLM_API_KEY, timeout=config.LLM_TIMEOUT)
    mensajes = historial + [{"role": "user", "content": mensaje}]
    catalogo = herramientas.herramientas_para(sesion)
    usadas: list[dict] = []

    for _ in range(MAX_ITERACIONES):
        respuesta = cliente.messages.create(
            model=config.LLM_MODELO,
            max_tokens=1200,
            system=instrucciones,
            tools=catalogo,
            messages=mensajes,
        )

        if respuesta.stop_reason != "tool_use":
            texto = "".join(b.text for b in respuesta.content if b.type == "text")
            mensajes.append({"role": "assistant", "content": texto})
            return texto, mensajes, usadas

        mensajes.append({"role": "assistant", "content": respuesta.content})
        resultados = []
        for bloque in respuesta.content:
            if bloque.type != "tool_use":
                continue
            salida = herramientas.ejecutar(bloque.name, bloque.input, sesion)
            usadas.append({"herramienta": bloque.name, "argumentos": bloque.input,
                           "resultado": salida})
            resultados.append({
                "type": "tool_result",
                "tool_use_id": bloque.id,
                "content": json.dumps(salida, ensure_ascii=False, default=str),
            })
        mensajes.append({"role": "user", "content": resultados})

    return ("No he podido completar la consulta en un numero razonable de pasos.",
            mensajes, usadas)
