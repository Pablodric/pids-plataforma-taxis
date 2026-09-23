"""
Motor de dialogo del chatbot (componente "gestion del dialogo").

Tiene dos modos, y el de reglas no es un adorno: es lo que permite que la
prueba de concepto siga funcionando en la demo aunque no haya clave de
API, se agote el credito o falle la red.

  - Modo LLM: el modelo decide que herramienta llamar (function calling).
  - Modo reglas: NLU por reglas (nlu.py) sobre las mismas herramientas.

En ambos modos las herramientas son exactamente las mismas y el aislamiento
por empresa se aplica igual, porque vive en la capa de datos, no aqui.

Estado de la conversacion (se guarda en Redis entre turnos):
  - historial: solo turnos de TEXTO {role, content: str}. Nunca se guardan
    bloques de herramientas: no son serializables tal cual y, recortados a
    mitad, dejan un tool_use sin su tool_result y la API del LLM rechaza
    toda la conversacion.
  - contexto: memoria del modo reglas (ultima consulta, para entender
    "¿y en Brooklyn?", y una escritura pendiente de confirmar).
"""

import json
import logging
import re
from datetime import datetime, timedelta

from app import config, herramientas, nlu, nlu_llm, repositorio
from app.auth import Sesion

# =====================================================================
# Formato
# =====================================================================


def _num(valor, decimales: int = 2) -> str:
    """Numero con formato espanol: 1.234,56"""
    if valor is None:
        return "—"
    texto = f"{float(valor):,.{decimales}f}"
    return texto.replace(",", "·").replace(".", ",").replace("·", ".")


def _usd(valor) -> str:
    return f"{_num(valor)} $"


def _valor_campo(campo: str, valor) -> str:
    if valor is None:
        return "—"
    if campo in ("importe_total", "propina"):
        return _usd(valor)
    if campo == "distancia":
        return f"{_num(valor)} millas"
    return _num(valor, 0)


def _plural(n, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _zona(nombre: str | None) -> str:
    """El catalogo de la TLC usa 'N/A', 'NV' y 'Unknown' para zonas sin dato."""
    if not nombre or nombre in ("N/A", "NV", "Unknown"):
        return "zona no registrada"
    return nombre


def _frase(texto: str) -> str:
    """Cierra con punto sin duplicarlo ('... S.L.' no lleva otro)."""
    return texto if texto.endswith(".") else texto + "."


def _cuando(iso: str) -> str:
    return iso[:19].replace("T", " a las ")[:-3] if iso else "—"


NOMBRE_CAMPO = repositorio.NOMBRE_CAMPO

# =====================================================================
# Plantillas de respuesta (modo reglas)
# =====================================================================


def redactar(intencion: str, datos: dict) -> str:
    """Lenguaje natural a partir del resultado de una herramienta."""
    if "error" in datos:
        return f"No he podido completarlo: {datos['error']}."

    aviso = datos.get("aviso_correcciones")
    sufijo = f"\n\n⚠ {aviso}" if aviso else ""

    if intencion == "resumen_metricas":
        f = datos["filtros"]
        ambito = []
        if f["borough"] != "todos":
            ambito.append(f"en {f['borough']}")
        if f["fecha"] != "todas":
            ambito.append(f"el {f['fecha']}")
        donde = (" " + " ".join(ambito)) if ambito else ""
        if not datos["num_viajes"]:
            return f"No hay viajes vigentes{donde}."
        sujeto = ("La plataforma tiene" if datos.get("alcance") == "todas las empresas"
                  else "Tu empresa tiene")
        return (
            f"{sujeto} **{datos['num_viajes']} viajes**{donde}.\n"
            f"- Importe medio: {_usd(datos['importe_medio'])}\n"
            f"- Facturación total: {_usd(datos['importe_total'])}\n"
            f"- Propina media: {_usd(datos['propina_media'])}\n"
            f"- Distancia media: {_num(datos['distancia_media'])} millas" + sufijo
        )

    if intencion == "metricas_por_zona":
        f = datos.get("filtros", {})
        filtro = []
        if f.get("borough", "todos") != "todos":
            filtro.append(f"en {f['borough']}")
        if f.get("fecha", "todas") != "todas":
            filtro.append(f"el {f['fecha']}")
        if not datos["zonas"]:
            return "No hay viajes vigentes" + (" " + " ".join(filtro) if filtro else "") + "."
        lineas = [
            f"{i}. {_zona(z['zona'])} ({z['borough']}): {_plural(z['num_viajes'], 'viaje')}, "
            f"{_usd(z['importe_medio'])} de media"
            for i, z in enumerate(datos["zonas"], 1)
        ]
        cabecera = "Zonas de recogida con más viajes" + (
            " " + " ".join(filtro) if filtro else "") + ":"
        return cabecera + "\n" + "\n".join(lineas) + sufijo

    if intencion == "metricas_por_borough":
        if not datos["boroughs"]:
            return "No hay viajes vigentes."
        total = sum(b["num_viajes"] for b in datos["boroughs"]) or 1
        lineas = [
            f"- {b['borough']}: {_plural(b['num_viajes'], 'viaje')} "
            f"({100 * b['num_viajes'] / total:.0f} %), {_usd(b['importe_total'])} "
            f"— media {_usd(b['importe_medio'])}"
            for b in datos["boroughs"]
        ]
        return "Reparto por distrito de recogida:\n" + "\n".join(lineas) + sufijo

    if intencion == "viajes_por_hora":
        horas = datos["horas"]
        pico = max(horas, key=lambda h: h["num_viajes"], default=None)
        if not pico:
            return "No hay datos horarios disponibles."
        total = sum(h["num_viajes"] for h in horas) or 1
        lineas = [f"- {h['hora']:02d}:00 → {_plural(h['num_viajes'], 'viaje')}" for h in horas]
        texto = (f"La hora punta es las **{pico['hora']:02d}:00**, con {pico['num_viajes']} "
                 f"viajes ({100 * pico['num_viajes'] / total:.0f} % del total).\n\n"
                 "Distribución:\n" + "\n".join(lineas))
        if len(horas) <= 4:
            texto += ("\n\nOjo: la muestra cargada cubre muy pocas horas (es el arranque "
                      "del 1 de enero de 2020), así que esta distribución no es "
                      "representativa de un día normal.")
        return texto

    if intencion == "reparto_pagos":
        total = sum(p["num_viajes"] for p in datos["pagos"]) or 1
        lineas = [
            f"- {p['etiqueta']}: {_plural(p['num_viajes'], 'viaje')} "
            f"({100 * p['num_viajes'] / total:.0f} %), propina media {_usd(p['propina_media'])}"
            for p in datos["pagos"]
        ]
        texto = "Reparto por método de pago:\n" + "\n".join(lineas)
        if any(p["etiqueta"] == "Efectivo" for p in datos["pagos"]):
            texto += ("\n\nLas propinas en efectivo no quedan registradas en el "
                      "taxímetro, por eso ahí la propina media es 0 o casi 0.")
        return texto

    if intencion == "listar_viajes":
        if not datos["viajes"]:
            return "No hay viajes."
        lineas = []
        for v in datos["viajes"]:
            marca = ""
            if v["cancelado"]:
                marca = " · CANCELADO"
            elif v["corregido"]:
                marca = f" · corregido (original {_usd(v['importe_original'])})"
            lineas.append(
                f"- Viaje {v['id']} ({_zona(v['zona_origen'])}): "
                f"{_usd(v['importe_total'])}{marca}"
            )
        return "Últimos viajes:\n" + "\n".join(lineas)

    if intencion == "detalle_viaje":
        c = datos["campos"]
        estado = ("**CANCELADO** (no cuenta en las métricas)" if datos["cancelado"]
                  else f"corregido ({datos['num_correcciones']} cambio/s)"
                  if datos["num_correcciones"] else "tal y como se ingirió")
        lineas = [
            f"Viaje **{datos['viaje_id']}** · {_cuando(datos['fecha'])} · "
            f"{_zona(datos['zona_origen'])} ({datos['borough_origen'] or '?'}) → "
            f"{_zona(datos['zona_destino'])}",
            f"Estado: {estado}. Pago: {datos['tipo_pago']}.",
        ]
        for campo in repositorio.CAMPOS_CORREGIBLES:
            v = c[campo]
            linea = f"- {NOMBRE_CAMPO[campo].capitalize()}: {_valor_campo(campo, v['vigente'])}"
            if v["corregido"]:
                linea += f" (original: {_valor_campo(campo, v['original'])})"
            lineas.append(linea)
        if datos["historial"]:
            lineas.append("\nCambios registrados:")
            for h in reversed(datos["historial"]):
                lineas.append("- " + _linea_historial(h))
        return "\n".join(lineas)

    if intencion == "historial_correcciones":
        if not datos["correcciones"]:
            return ("No hay ninguna corrección ni cancelación registrada todavía, "
                    "así que las métricas reflejan los datos tal y como se ingirieron.")
        lineas = [f"- {_linea_historial(c)}" for c in datos["correcciones"]]
        extra = (f"\n\n(Se muestran {len(lineas)} de {datos['total']}.)"
                 if datos["total"] > len(lineas) else "")
        return ("Esto es lo que ha cambiado y por qué (más reciente primero):\n"
                + "\n".join(lineas) + extra)

    if intencion == "registrar_correccion":
        r = datos.get("resumen_actualizado") or {}
        return (
            f"Hecho. En el viaje {datos['viaje_id']}, {NOMBRE_CAMPO[datos['campo']]} pasa de "
            f"{_valor_campo(datos['campo'], datos['valor_original'])} a "
            f"**{_valor_campo(datos['campo'], datos['valor_nuevo'])}**. "
            f"El valor original queda en el historial.\n"
            f"Solo se ha recalculado el cubo del día y distrito del viaje. "
            f"Facturación total ahora: {_usd(r.get('importe_total'))}."
        )

    if intencion == "cancelar_viaje":
        r = datos.get("resumen_actualizado") or {}
        return (
            f"Hecho. El viaje {datos['viaje_id']} queda cancelado: deja de contar en las "
            f"métricas ({_usd(datos.get('importe_excluido'))} menos) pero sigue en el "
            f"historial con su motivo. Viajes vigentes ahora: {r.get('num_viajes', '—')}."
        )

    if intencion == "metricas_globales":
        lineas = [
            f"- {e['nombre']}: {_plural(e['num_viajes'], 'viaje')}, {_usd(e['importe_total'])} "
            f"(media {_usd(e['importe_medio'])}), {e['corregidos']} corregidos, "
            f"{e['cancelados']} cancelados"
            for e in datos["empresas"]
        ]
        return (f"Métricas globales de la plataforma ({datos['total_viajes']} viajes):\n"
                + "\n".join(lineas))

    return json.dumps(datos, ensure_ascii=False, default=str)


def _linea_historial(c: dict) -> str:
    cuando = _cuando(c["aplicada_en"])
    if c["tipo"] == "cancelacion":
        return (f"Viaje {c['viaje_id']} cancelado el {cuando} por {c['aplicada_por']} "
                f"({_usd(c['valor_original'])} dejan de contar). Motivo: {c['motivo']}")
    return (f"Viaje {c['viaje_id']}: {NOMBRE_CAMPO.get(c['campo'], c['campo'])} de "
            f"{_valor_campo(c['campo'], c['valor_original'])} a "
            f"{_valor_campo(c['campo'], c['valor_nuevo'])}, el {cuando} por "
            f"{c['aplicada_por']}. Motivo: {c['motivo']}")


# =====================================================================
# Modo reglas: gestion del dialogo
# =====================================================================


def _ayuda(sesion: Sesion) -> str:
    empresa = repositorio.nombre_empresa(sesion.empresa_id)
    ejemplo = "411"
    if sesion.puede("corregir"):
        try:
            viajes = repositorio.listar_viajes(sesion, 5)["viajes"]
            vivo = next((v for v in viajes if not v["cancelado"]), None)
            if vivo:
                ejemplo = str(vivo["id"])
        except Exception as exc:  # el ejemplo es cosmetico: nunca debe romper la ayuda
            logging.getLogger("pids").debug("sin viaje de ejemplo: %s", exc)
    alcance = ("toda la plataforma (rol auditor, solo lectura)"
               if sesion.puede("ver_global") else empresa)
    lineas = [
        f"Puedo consultar los datos de {_frase(alcance)} Prueba, por ejemplo:",
        "- Métricas: «¿cuántos viajes tenemos?», «tarifa media en Manhattan», "
        "«¿cuánto facturamos el 1 de enero?»",
        "- Demanda: «zonas con más viajes», «reparto por distrito», "
        "«¿a qué hora hay más demanda?», «¿cómo pagan los clientes?»",
        f"- Trazabilidad: «¿por qué ha cambiado esa cifra?», «detalle del viaje {ejemplo}»",
    ]
    if sesion.puede("corregir"):
        lineas.append(
            f"- Correcciones: «corrige el viaje {ejemplo}, importe 23,50», "
            f"«cancela el viaje {ejemplo}, el cliente anuló el servicio» "
            "(te pediré confirmación antes de escribir)"
        )
    if sesion.puede("ver_global"):
        lineas.append("- Plataforma: «compara las empresas de la plataforma»")
    lineas.append("Tras una consulta puedes afinar: «¿y en Brooklyn?», «¿y el 31 de diciembre?».")
    return "\n".join(lineas)


def _menciona_otra_empresa(t: str, sesion: Sesion) -> str | None:
    """
    Si el usuario nombra a otra empresa de la plataforma, se le dice
    explicitamente que no hay acceso, en vez de devolverle sus propias
    cifras como si nada. El dato no se filtra en ningun caso (el RLS ya
    lo impide), pero la respuesta es mucho mas clara.

    Solo cuentan las empresas con datos propios: el operador de la
    plataforma no es un cliente, y antes cualquier frase con la palabra
    "plataforma" disparaba la negativa.
    """
    for empresa in repositorio.catalogo_empresas():
        if empresa["id"] == sesion.empresa_id or not empresa["tiene_datos"]:
            continue
        nucleo = re.sub(r"\b(s\.?\s?a\.?|s\.?\s?l\.?(u\.?)?)(?=\s|$)", "", nlu.normalizar(empresa["nombre"]))
        nucleo = " ".join(re.sub(r"[^a-z0-9 ]", " ", nucleo).split())
        articulos = ("de", "del", "la", "las", "los", "el")
        sin_articulos = " ".join(p for p in nucleo.split() if p not in articulos)
        candidatos = {nucleo, sin_articulos, empresa["id"].replace("_", " ")}
        if any(c and re.search(rf"\b{re.escape(c)}\b", t) for c in candidatos):
            return empresa["nombre"].rstrip(".")
    if re.search(r"\b(otras? empresas?|la competencia|nuestros competidores|"
                 r"los demas|el resto de empresas)\b", t):
        return "otras empresas"
    return None


def _interpretar(mensaje: str) -> tuple[nlu.Interpretacion, str, str | None]:
    """
    Componente NLU enchufable: modelo local en Ollama si esta configurado y
    responde; si no, reglas. Devuelve (interpretacion, nlu usado, fallo).
    """
    if config.OLLAMA_ACTIVO:
        cliente = nlu_llm.cliente()
        modelo = cliente.modelo_activo()
        if modelo is None:
            return nlu.interpretar(mensaje), "reglas", "OllamaNoDisponible"
        try:
            marco = cliente.interpretar(mensaje, modelo)
            return nlu.desde_marco(marco, mensaje), f"ollama:{modelo}", None
        except Exception as exc:
            # Tiempo agotado, JSON invalido, modelo descargado a medias...
            return nlu.interpretar(mensaje), "reglas", type(exc).__name__
    return nlu.interpretar(mensaje), "reglas", None


def _responder_con_reglas(mensaje: str, contexto: dict, sesion: Sesion) -> dict:
    """
    Gestion del dialogo sobre la salida del NLU (reglas u Ollama).
    Devuelve {texto, usadas, contexto, pendiente, nlu, fallo_nlu}.
    'contexto' es la memoria del dialogo entre turnos.
    """
    it, nlu_usado, fallo_nlu = _interpretar(mensaje)
    t = it.texto
    contexto = dict(contexto or {})
    pendiente = contexto.pop("pendiente", None)
    segun_modelo = it.argumentos.get("_intencion_modelo")
    afirma = nlu.es_afirmacion(t) or segun_modelo == "afirmar"
    niega = not afirma and (nlu.es_negacion(t) or segun_modelo == "negar")

    def fin(texto, usadas=None, nuevo_pendiente=None):
        if nuevo_pendiente:
            contexto["pendiente"] = nuevo_pendiente
        return {"texto": texto, "usadas": usadas or [], "contexto": contexto,
                "pendiente": bool(nuevo_pendiente), "nlu": nlu_usado, "fallo_nlu": fallo_nlu}

    # 1. Respuesta a una confirmacion pendiente
    if pendiente:
        if afirma:
            accion, args = pendiente["accion"], pendiente["argumentos"]
            datos = herramientas.ejecutar(accion, args, sesion)
            return fin(redactar(accion, datos),
                       [{"herramienta": accion, "argumentos": args, "resultado": datos}])
        if niega:
            return fin("De acuerdo, no he registrado nada.")
        # Cualquier otra cosa: la escritura pendiente se descarta y el
        # mensaje se atiende como uno nuevo.
    elif (afirma or niega) and len(t.split()) <= 3:
        return fin("No tengo ninguna operación pendiente de confirmar. "
                   "¿Qué quieres consultar?")

    # 2. Datos de otra empresa (E7)
    if not sesion.puede("ver_global"):
        ajena = _menciona_otra_empresa(t, sesion)
        if ajena:
            return fin(
                f"No tengo acceso a los datos de {ajena}. Cada empresa de la plataforma "
                f"solo puede consultar los suyos, y tu sesión pertenece a "
                f"{_frase(repositorio.nombre_empresa(sesion.empresa_id))} "
                "Puedo darte cualquier métrica de tu empresa."
            )
    elif _menciona_otra_empresa(t, sesion) and it.intencion in (None, "resumen_metricas"):
        it.intencion = "metricas_globales"

    intencion = it.intencion

    # 2b. Viaje en el tiempo: "¿cuánto facturábamos antes de la última corrección?"
    #     Regla determinista (no toca el contrato del NLU con el que se entreno el modelo).
    if re.search(r"\bantes de(l| la)? (ultim[oa] )?(correccion|cancelacion|cambio)\b", t):
        return _antes_de_la_ultima(sesion, fin)

    # 3. Seguimiento: "¿y en Brooklyn?" reutiliza la consulta anterior
    ultima = contexto.get("ultima") or {}
    if (ultima.get("intencion") in nlu.INTENCIONES_FILTRABLES
            and (it.borough or it.fecha_partes)
            and (intencion is None or (nlu.es_seguimiento(t) and intencion == "resumen_metricas"))):
        intencion = ultima["intencion"]
    elif intencion is None and (it.borough or it.fecha_partes):
        intencion = "resumen_metricas"

    # 4. Conversacion
    if intencion == "saludo":
        return fin(f"¡Hola! {_ayuda(sesion)}")
    if intencion == "ayuda":
        return fin(_ayuda(sesion))
    if intencion == "gracias":
        return fin("¡A ti! Si necesitas otra cifra, pregunta.")
    if intencion is None:
        return fin("No estoy seguro de haberte entendido. " + _ayuda(sesion))

    # 5. Escrituras (E6): se previsualizan y se pide confirmacion
    if intencion in herramientas.ESCRITURAS:
        return _preparar_escritura(intencion, it, sesion, fin)

    # 6. Consultas
    if intencion == "metricas_globales" and not sesion.puede("ver_global"):
        return fin("Las métricas globales de la plataforma solo están disponibles "
                   "para el rol auditor. Puedo darte las de tu empresa.")

    argumentos: dict = {}
    if intencion in nlu.INTENCIONES_FILTRABLES:
        base = ultima.get("argumentos", {}) if (
            ultima.get("intencion") == intencion and nlu.es_seguimiento(t)) else {}
        argumentos = dict(base)
        if it.borough:
            argumentos["borough"] = it.borough
        if it.fecha_partes:
            fecha = nlu.resolver_fecha(it.fecha_partes, repositorio.fechas_disponibles(sesion))
            if fecha is None:
                return fin("Esa fecha no existe. Prueba con otro formato, por ejemplo "
                           "«1 de enero de 2020» o «2020-01-01».")
            argumentos["fecha"] = fecha.isoformat()
    if intencion in ("detalle_viaje", "historial_correcciones") and it.viaje_id is not None:
        argumentos["viaje_id"] = it.viaje_id
    if intencion == "listar_viajes":
        argumentos["limite"] = 10

    datos = herramientas.ejecutar(intencion, argumentos, sesion)
    contexto["ultima"] = {"intencion": intencion, "argumentos": argumentos}
    return fin(redactar(intencion, datos),
               [{"herramienta": intencion, "argumentos": argumentos, "resultado": datos}])


def _antes_de_la_ultima(sesion: Sesion, fin):
    ultimas = repositorio.historial_correcciones(sesion, None, 1)["correcciones"]
    if not ultimas:
        return fin("Todavía no hay correcciones: las cifras actuales son las de la ingesta.")
    c = ultimas[0]
    momento = repositorio.momento_de_correccion(sesion, c["id"])
    d = repositorio.resumen_en(sesion, momento - timedelta(microseconds=1))
    antes, ahora, dif = d["en_ese_instante"], d["ahora"], d["diferencia"]
    que = "cancelación" if c["tipo"] == "cancelacion" else f"corrección de {NOMBRE_CAMPO[c['campo']]}"
    signo = "+" if dif["importe_total"] >= 0 else ""
    return fin(
        f"Justo antes de la última {que} (viaje {c['viaje_id']}, {_cuando(c['aplicada_en'])}), "
        f"reconstruido desde el log de eventos:\n"
        f"- Viajes vigentes: {antes['num_viajes']} (ahora {ahora['num_viajes']})\n"
        f"- Facturación: {_usd(antes['importe_total'])} (ahora {_usd(ahora['importe_total'])}, "
        f"{signo}{_usd(dif['importe_total'])})\n"
        f"- Importe medio: {_usd(antes['importe_medio'])} (ahora {_usd(ahora['importe_medio'])})",
        [{"herramienta": "metricas_en_el_tiempo", "argumentos": {"antes_de_correccion": c["id"]},
          "resultado": d}],
    )


def _preparar_escritura(intencion: str, it: nlu.Interpretacion, sesion: Sesion, fin):
    if not sesion.puede("corregir"):
        if sesion.puede("ver_global"):
            return fin("El rol auditor es de solo lectura: puede verlo todo, pero no "
                       "modificar datos de ninguna empresa. Las correcciones las "
                       "registra un operador de cada empresa.")
        return fin(f"Tu rol ({sesion.rol}) solo permite consultar. Las correcciones y "
                   "cancelaciones las registra un operador de tu empresa.")

    if it.viaje_id is None:
        return fin("¿De qué viaje se trata? Indícame su número, por ejemplo: "
                   "«corrige el viaje 411, importe 23,50».")

    motivo = it.motivo or f"Solicitado por chat: «{it.original[:200]}»"

    if intencion == "cancelar_viaje":
        ficha = repositorio.detalle_viaje(sesion, it.viaje_id)
        if "error" in ficha:
            return fin(redactar(intencion, ficha))
        if ficha["cancelado"]:
            return fin(f"El viaje {it.viaje_id} ya estaba cancelado.")
        args = {"viaje_id": it.viaje_id, "motivo": motivo}
        return fin(
            f"Voy a **cancelar el viaje {it.viaje_id}** ({_zona(ficha['zona_origen'])}, "
            f"{_usd(ficha['campos']['importe_total']['vigente'])}). Dejará de contar en "
            f"las métricas, pero seguirá en el historial.\n- Motivo: {motivo}\n\n"
            "¿Confirmas? (sí / no)",
            nuevo_pendiente={"accion": intencion, "argumentos": args},
        )

    # registrar_correccion
    if it.campo is None:
        return fin(f"¿Qué dato del viaje {it.viaje_id} quieres corregir? Puedo corregir "
                   "importe total, propina, distancia o pasajeros. Por ejemplo: "
                   f"«corrige el viaje {it.viaje_id}, propina 3,50».")
    if it.valor is None:
        return fin(f"¿Cuál es el valor correcto de {NOMBRE_CAMPO[it.campo]} para el viaje "
                   f"{it.viaje_id}? Por ejemplo: «corrige el viaje {it.viaje_id}, "
                   f"{NOMBRE_CAMPO[it.campo]} 12,50».")

    previa = repositorio.previsualizar_correccion(sesion, it.viaje_id, it.campo, it.valor)
    if "error" in previa:
        return fin(redactar(intencion, previa))
    args = {"viaje_id": it.viaje_id, "campo": it.campo, "valor_nuevo": it.valor,
            "motivo": motivo}
    return fin(
        f"Voy a registrar esta corrección en el **viaje {it.viaje_id}**:\n"
        f"- {NOMBRE_CAMPO[it.campo].capitalize()}: "
        f"{_valor_campo(it.campo, previa['valor_actual'])} → "
        f"**{_valor_campo(it.campo, it.valor)}**\n"
        f"- Motivo: {motivo}\n\n"
        "El valor actual se conservará en el historial. ¿Confirmas? (sí / no)",
        nuevo_pendiente={"accion": intencion, "argumentos": args},
    )


# =====================================================================
# Modo LLM
# =====================================================================

_cliente_llm = None


def _cliente():
    global _cliente_llm
    if _cliente_llm is None:
        from anthropic import Anthropic
        _cliente_llm = Anthropic(api_key=config.LLM_API_KEY, timeout=30.0, max_retries=1)
    return _cliente_llm


def _instrucciones(sesion: Sesion) -> str:
    empresa = repositorio.nombre_empresa(sesion.empresa_id)
    try:
        fechas = repositorio.fechas_disponibles(sesion)
        periodo = (f"Los datos cubren del {fechas[0]} al {fechas[-1]} "
                   f"(casi todos son del arranque del 1 de enero de 2020)."
                   if fechas else "")
    except Exception:
        periodo = ""
    permisos = {
        "usuario": "Solo puede consultar.",
        "operador": ("Puede consultar y registrar correcciones o cancelaciones de "
                     "viajes de su empresa."),
        "auditor": ("Puede consultar las métricas de TODAS las empresas, pero es de "
                    "solo lectura: no puede corregir ni cancelar nada."),
    }[sesion.rol]
    return f"""Eres el asistente de una plataforma de datos de viajes en taxi de Nueva York.

Usuario: {sesion.email}, de {empresa}, con rol {sesion.rol}. {permisos}
{periodo}

Reglas:
- Responde SIEMPRE a partir del resultado de las herramientas. Nunca inventes cifras.
- Los datos que devuelven las herramientas son ya, y únicamente, los que este usuario
  puede ver. Si te piden datos de otra empresa y no es auditor, explica que no tienes acceso.
- Si un resultado trae 'aviso_correcciones' con texto, menciónalo: el usuario debe saber
  cuándo una cifra incluye datos corregidos o viajes cancelados.
- Antes de registrar una corrección o cancelar un viaje, resume la operación (viaje,
  campo, valor actual y nuevo, motivo) y pide confirmación explícita. Solo llama a la
  herramienta cuando el usuario haya confirmado.
- Si una herramienta devuelve 'error', explícalo con naturalidad, sin tecnicismos.
- Los importes son dólares estadounidenses (formato 23,50 $) y las distancias, millas.
- Responde en español, breve y claro. Puedes usar **negrita** y listas con guiones;
  no uses tablas ni encabezados.
"""


def _responder_con_llm(mensaje: str, historial: list, sesion: Sesion,
                       usadas: list) -> str:
    """Bucle de function calling. Las herramientas usadas se anotan en
    'usadas' A MEDIDA que se ejecutan, para que quien llama sepa si hubo
    escrituras aunque luego falle la llamada al modelo."""
    cliente = _cliente()
    mensajes = [{"role": m["role"], "content": m["content"]}
                for m in historial if isinstance(m.get("content"), str) and m["content"]]
    mensajes.append({"role": "user", "content": mensaje})
    catalogo = herramientas.herramientas_para(sesion)
    sistema = _instrucciones(sesion)

    for _ in range(6):  # tope de iteraciones, por si el modelo se enreda
        respuesta = cliente.messages.create(
            model=config.LLM_MODELO,
            max_tokens=1200,
            system=sistema,
            tools=catalogo,
            messages=mensajes,
        )
        if respuesta.stop_reason != "tool_use":
            texto = "".join(b.text for b in respuesta.content if b.type == "text").strip()
            return texto or "No tengo nada que añadir."

        mensajes.append({"role": "assistant",
                         "content": [b.model_dump(exclude_none=True) for b in respuesta.content]})
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
                "is_error": "error" in salida,
            })
        mensajes.append({"role": "user", "content": resultados})

    return "No he podido completar la consulta en un número razonable de pasos."


# =====================================================================
# Punto de entrada
# =====================================================================


def responder(mensaje: str, historial: list, sesion: Sesion,
              contexto: dict | None = None) -> dict:
    inicio = datetime.now()
    modo = "llm" if config.LLM_ACTIVO else "reglas"
    degradado = None
    nlu_usado = "llm" if config.LLM_ACTIVO else "reglas"
    usadas: list[dict] = []
    pendiente = False
    contexto = contexto or {}

    if config.LLM_ACTIVO:
        try:
            texto = _responder_con_llm(mensaje, historial, sesion, usadas)
        except Exception as exc:
            degradado = type(exc).__name__
            escrituras = [u for u in usadas
                          if u["herramienta"] in herramientas.ESCRITURAS
                          and "error" not in u["resultado"]]
            if escrituras:
                # El modelo ya escribio y luego fallo: repetir el mensaje en
                # modo reglas duplicaria la correccion. Se informa de lo hecho.
                texto = "\n\n".join(redactar(u["herramienta"], u["resultado"])
                                    for u in escrituras)
            else:
                modo = "reglas"
                usadas = []
                r = _responder_con_reglas(mensaje, contexto, sesion)
                texto, usadas, contexto, pendiente = (
                    r["texto"], r["usadas"], r["contexto"], r["pendiente"])
                nlu_usado = r["nlu"]
    else:
        r = _responder_con_reglas(mensaje, contexto, sesion)
        texto, usadas, contexto, pendiente = (
            r["texto"], r["usadas"], r["contexto"], r["pendiente"])
        nlu_usado = r["nlu"]
        if nlu_usado.startswith("ollama:"):
            modo = "ollama"
        elif config.OLLAMA_ACTIVO:
            degradado = r["fallo_nlu"]   # Ollama configurado pero no respondio

    historial = historial + [{"role": "user", "content": mensaje},
                             {"role": "assistant", "content": texto}]
    return {
        "respuesta": texto,
        "historial": historial,
        "contexto": contexto,
        "pendiente_confirmacion": pendiente,
        "herramientas_usadas": usadas,
        "modo": modo,
        "nlu": nlu_usado,
        "degradado_desde_llm": degradado,
        "latencia_ms": int((datetime.now() - inicio).total_seconds() * 1000),
    }
