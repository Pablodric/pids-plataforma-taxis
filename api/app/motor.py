"""
Motor de dialogo del chatbot.

Este modulo decide QUE motor responde y contiene el modo reglas. Los
motores basados en LLM viven en proveedor_anthropic.py y
proveedor_openai.py.

Hay cuatro modos, configurables con LLM_PROVEEDOR en el .env:

  reglas     NLU propio por palabras clave. Sin dependencias ni coste.
  ollama     Modelo abierto en un contenedor propio. Sin clave.
  openai     API compatible con OpenAI (Groq, Gemini, OpenAI).
  anthropic  API de Anthropic.

Lo importante: en los cuatro se usan LAS MISMAS herramientas y la misma
capa de datos. El aislamiento entre empresas (E7) y las correcciones
(E6) no dependen del motor, porque viven en la base de datos y en la
capa de acceso, no en el modelo. Cambiar de proveedor no puede romper
ninguna de las dos restricciones.

Ademas, si el motor elegido falla en caliente (sin credito, red caida,
contenedor de Ollama parado), la peticion se resuelve igualmente en modo
reglas en lugar de devolver un error.
"""

import json
import re
import unicodedata
from datetime import datetime

from app import config, herramientas
from app.auth import Sesion

INSTRUCCIONES = """Eres el asistente de una plataforma de datos de viajes en taxi.

Reglas:
- Responde SIEMPRE a partir del resultado de las herramientas. Nunca inventes cifras.
- Los datos que devuelven las herramientas son ya, y unicamente, los de la empresa
  del usuario. Si te piden datos de otra empresa, explica que no tienes acceso.
- Si un resultado trae el campo 'aviso_correcciones' con texto, menciona ese aviso
  en tu respuesta: el usuario debe saber cuando una cifra incluye datos corregidos.
- Si una herramienta devuelve 'error', explicalo con naturalidad, sin tecnicismos.
- Importes en euros, con dos decimales. Respuestas breves y en espanol.
"""


# =====================================================================
# Modo reglas (respaldo)
# =====================================================================

BOROUGHS = {
    "manhattan": "Manhattan", "brooklyn": "Brooklyn", "queens": "Queens",
    "bronx": "Bronx", "staten island": "Staten Island", "staten": "Staten Island",
}


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _detectar_intencion(t: str) -> str:
    if any(p in t for p in ("por que", "porque ha cambiado", "corregid", "correccion",
                            "cancelad", "historial", "ha variado", "cambio la cifra")):
        return "historial_correcciones"
    if any(p in t for p in ("global", "todas las empresas", "plataforma entera",
                            "comparativa entre empresas")):
        return "metricas_globales"
    if any(p in t for p in ("zona", "barrio", "donde se recogen", "top zonas")):
        return "metricas_por_zona"
    if any(p in t for p in ("distrito", "borough", "reparto por distrito")):
        return "metricas_por_borough"
    if any(p in t for p in ("hora", "franja", "momento del dia", "cuando hay mas")):
        return "viajes_por_hora"
    if any(p in t for p in ("pago", "tarjeta", "efectivo", "propina")):
        return "reparto_pagos"
    if any(p in t for p in ("lista", "listar", "ultimos viajes", "ver viajes",
                            "que viajes")):
        return "listar_viajes"
    return "resumen_metricas"


def _extraer_argumentos(t: str, intencion: str) -> dict:
    args: dict = {}
    for clave, nombre in BOROUGHS.items():
        if clave in t:
            args["borough"] = nombre
            break
    fecha = re.search(r"(\d{4}-\d{2}-\d{2})", t)
    if fecha:
        args["fecha"] = fecha.group(1)
    viaje = re.search(r"viaje\s*#?\s*(\d+)", t)
    if viaje and intencion == "historial_correcciones":
        args["viaje_id"] = int(viaje.group(1))
    if intencion not in ("resumen_metricas", "metricas_por_zona"):
        args.pop("borough", None)
        args.pop("fecha", None)
    return args


def _redactar(intencion: str, datos: dict) -> str:
    """Plantillas de lenguaje natural sobre el resultado de la herramienta."""
    if "error" in datos:
        return f"No he podido completar la consulta: {datos['error']}"

    aviso = datos.get("aviso_correcciones")
    sufijo = f"\n\nNota: {aviso}" if aviso else ""

    if intencion == "resumen_metricas":
        if not datos["num_viajes"]:
            return "No hay viajes registrados para ese filtro."
        f = datos["filtros"]
        ambito = []
        if f["borough"] != "todos":
            ambito.append(f"en {f['borough']}")
        if f["fecha"] != "todas":
            ambito.append(f"el {f['fecha']}")
        donde = (" " + " ".join(ambito)) if ambito else ""
        return (
            f"Tu empresa tiene {datos['num_viajes']} viajes{donde}.\n"
            f"- Importe medio: {datos['importe_medio']:.2f} EUR\n"
            f"- Importe total: {datos['importe_total']:.2f} EUR\n"
            f"- Distancia media: {datos['distancia_media']:.2f} millas" + sufijo
        )

    if intencion == "metricas_por_zona":
        if not datos["zonas"]:
            return "No hay zonas con viajes para ese filtro."
        lineas = [
            f"{i}. {z['zona']} ({z['borough']}): {z['num_viajes']} viajes, "
            f"{z['importe_medio']:.2f} EUR de media"
            for i, z in enumerate(datos["zonas"], 1)
        ]
        return "Zonas de recogida con mas viajes:\n" + "\n".join(lineas) + sufijo

    if intencion == "metricas_por_borough":
        lineas = [
            f"- {b['borough']}: {b['num_viajes']} viajes, "
            f"{b['importe_total']:.2f} EUR ({b['importe_medio']:.2f} de media)"
            for b in datos["boroughs"]
        ]
        return "Reparto por distrito:\n" + "\n".join(lineas) + sufijo

    if intencion == "viajes_por_hora":
        pico = max(datos["horas"], key=lambda h: h["num_viajes"], default=None)
        if not pico:
            return "No hay datos horarios disponibles."
        lineas = [f"- {h['hora']:02d}:00 -> {h['num_viajes']} viajes" for h in datos["horas"]]
        return (f"La hora punta es las {pico['hora']:02d}:00 con {pico['num_viajes']} "
                f"viajes.\n\nDistribucion completa:\n" + "\n".join(lineas))

    if intencion == "reparto_pagos":
        lineas = [
            f"- {p['etiqueta']}: {p['num_viajes']} viajes, "
            f"propina media {p['propina_media']:.2f} EUR"
            for p in datos["pagos"]
        ]
        return "Reparto por metodo de pago:\n" + "\n".join(lineas)

    if intencion == "listar_viajes":
        lineas = []
        for v in datos["viajes"]:
            marca = ""
            if v["cancelado"]:
                marca = "  [CANCELADO]"
            elif v["corregido"]:
                marca = f"  [CORREGIDO, original {v['importe_original']:.2f}]"
            lineas.append(
                f"- Viaje {v['id']} ({v['zona_origen']}): "
                f"{v['importe_total']:.2f} EUR{marca}"
            )
        return "Ultimos viajes de tu empresa:\n" + "\n".join(lineas)

    if intencion == "historial_correcciones":
        if not datos["correcciones"]:
            return ("No hay ninguna correccion ni cancelacion registrada todavia, "
                    "asi que las metricas reflejan los datos tal y como se ingirieron.")
        lineas = []
        for c in datos["correcciones"]:
            cuando = c["aplicada_en"][:19].replace("T", " ")
            if c["tipo"] == "cancelacion":
                lineas.append(
                    f"- Viaje {c['viaje_id']} cancelado el {cuando} por {c['aplicada_por']}. "
                    f"Motivo: {c['motivo']}. Importe que dejo de contar: "
                    f"{c['valor_original']:.2f} EUR"
                )
            else:
                lineas.append(
                    f"- Viaje {c['viaje_id']}: {c['campo']} paso de "
                    f"{c['valor_original']:.2f} a {c['valor_nuevo']:.2f} el {cuando} "
                    f"por {c['aplicada_por']}. Motivo: {c['motivo']}"
                )
        return ("Esto es lo que ha cambiado y por que:\n" + "\n".join(lineas))

    if intencion == "metricas_globales":
        lineas = [
            f"- {e['empresa']}: {e['num_viajes']} viajes, {e['importe_total']:.2f} EUR"
            for e in datos["empresas"]
        ]
        return (f"Metricas globales ({datos['total_viajes']} viajes en total):\n"
                + "\n".join(lineas))

    return json.dumps(datos, ensure_ascii=False, default=str)


def _menciona_otra_empresa(texto_normalizado: str, sesion: Sesion) -> str | None:
    """
    Si el usuario nombra a otra empresa de la plataforma, se le dice
    explicitamente que no hay acceso, en vez de devolverle sus propias
    cifras como si nada. El dato no se filtra en ningun caso (el RLS ya
    lo impide), pero la respuesta es mucho mas clara.
    """
    from app import repositorio

    for empresa_id, nombre in repositorio.catalogo_empresas().items():
        if empresa_id == sesion.empresa_id:
            continue
        # Se compara el nucleo del nombre, sin forma juridica ni puntuacion:
        # "Movilidad Sur S.A." -> "movilidad sur", que es como lo escribe
        # la gente en el chat.
        nucleo = re.sub(r"\b(s\.?a\.?|s\.?l\.?|sa|sl)\b", "", _normalizar(nombre))
        nucleo = re.sub(r"[^a-z0-9 ]", " ", nucleo)
        nucleo = " ".join(nucleo.split())
        candidatos = {nucleo, empresa_id, empresa_id.replace("_", " ")}
        if any(c and c in texto_normalizado for c in candidatos):
            return nombre.rstrip(".")
    return None


def _responder_con_reglas(mensaje: str, historial: list, sesion: Sesion):
    t = _normalizar(mensaje)

    ajena = _menciona_otra_empresa(t, sesion)
    if ajena:
        texto = (
            f"No tengo acceso a los datos de {ajena}. Cada empresa de la "
            f"plataforma solo puede consultar los suyos, y tu sesion pertenece "
            f"a {sesion.empresa_id}. Puedo darte cualquier metrica de tu empresa."
        )
        historial = historial + [{"role": "user", "content": mensaje},
                                 {"role": "assistant", "content": texto}]
        return texto, historial, []

    intencion = _detectar_intencion(t)

    # Control de rol antes de ejecutar, igual que en modo LLM.
    if intencion == "metricas_globales" and not sesion.puede("auditor"):
        texto = ("Las metricas globales de la plataforma solo estan disponibles "
                 "para el rol auditor. Puedo darte las de tu empresa.")
        historial = historial + [{"role": "user", "content": mensaje},
                                 {"role": "assistant", "content": texto}]
        return texto, historial, []

    argumentos = _extraer_argumentos(t, intencion)
    datos = herramientas.ejecutar(intencion, argumentos, sesion)
    texto = _redactar(intencion, datos)

    historial = historial + [{"role": "user", "content": mensaje},
                             {"role": "assistant", "content": texto}]
    return texto, historial, [{"herramienta": intencion, "argumentos": argumentos,
                               "resultado": datos}]


# =====================================================================
# Punto de entrada
# =====================================================================

def _motor_llm(mensaje: str, historial: list, sesion: Sesion):
    """Encamina hacia el proveedor configurado."""
    if config.PROVEEDOR == "anthropic":
        from app import proveedor_anthropic
        return proveedor_anthropic.responder(mensaje, historial, sesion, INSTRUCCIONES)
    from app import proveedor_openai
    return proveedor_openai.responder(mensaje, historial, sesion, INSTRUCCIONES)


def responder(mensaje: str, historial: list, sesion: Sesion) -> dict:
    inicio = datetime.now()
    modo = config.PROVEEDOR
    degradado = None

    if config.LLM_ACTIVO:
        try:
            texto, historial, usadas = _motor_llm(mensaje, historial, sesion)
        except Exception as exc:
            # El fallo de un servicio externo no deja sin servicio a la
            # plataforma: se responde en modo reglas y se deja constancia
            # en la respuesta para que sea visible que hubo degradacion.
            modo, degradado = "reglas", f"{config.PROVEEDOR}: {type(exc).__name__}"
            texto, historial, usadas = _responder_con_reglas(mensaje, historial, sesion)
    else:
        texto, historial, usadas = _responder_con_reglas(mensaje, historial, sesion)

    return {
        "respuesta": texto,
        "historial": historial,
        "herramientas_usadas": usadas,
        "modo": modo,
        "modelo": config.LLM_MODELO if modo not in ("reglas",) else None,
        "degradado_desde": degradado,
        "latencia_ms": int((datetime.now() - inicio).total_seconds() * 1000),
    }
