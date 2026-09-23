"""
NLU por reglas: intencion y entidades a partir del texto del usuario.

Es el componente "NLU" del esquema del agente conversacional cuando no
hay LLM disponible. No pretende competir con un modelo de lenguaje, pero
cubre todos los casos de uso de la plataforma, incluidas las escrituras
de E6 (corregir y cancelar), con frases naturales en espanol:

    "¿cuánto facturamos el 1 de enero en Manhattan?"
    "el viaje 411 tenía mal la tarifa, eran 23,50"
    "cancela el viaje 88, el cliente anuló el servicio"
    "¿y en Brooklyn?"                  (seguimiento de la pregunta anterior)

Todo trabaja sobre texto normalizado (minusculas y sin tildes). La
normalizacion conserva la longitud del texto, de modo que las posiciones
de una coincidencia sirven tambien para recortar el texto original (asi
el motivo de una correccion se guarda con sus tildes y mayusculas).
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

# ---------------------------------------------------------------------
# Normalizacion
# ---------------------------------------------------------------------


def normalizar(texto: str) -> str:
    """Minusculas y sin tildes, conservando la longitud del texto NFC."""
    salida = []
    for c in unicodedata.normalize("NFC", texto):
        base = unicodedata.normalize("NFD", c.lower())
        base = "".join(x for x in base if unicodedata.category(x) != "Mn")
        salida.append(base if len(base) == 1 else c.lower()[:1] or " ")
    return "".join(salida)


def _hay(patron: str, t: str) -> bool:
    return re.search(patron, t) is not None


# ---------------------------------------------------------------------
# Entidades
# ---------------------------------------------------------------------

BOROUGHS = [
    (r"\bstaten( island)?\b", "Staten Island"),
    (r"\bmanhattan\b", "Manhattan"),
    (r"\bbrooklyn\b", "Brooklyn"),
    (r"\bqueens\b", "Queens"),
    (r"\b(the )?bronx\b", "Bronx"),
    (r"\b(ewr|newark)\b", "EWR"),
]

MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}
_RE_MES = "|".join(MESES)

# Sinonimos de los campos corregibles
CAMPOS = [
    (r"\b(propinas?)\b", "propina"),
    (r"\b(pasajeros?|personas|ocupantes|viajeros)\b", "pasajeros"),
    (r"\b(distancias?|millas?|recorrido|kilometros|km)\b", "distancia"),
    (r"\b(importes?( total)?|tarifas?|precios?|total|cobro|cobrado|cobramos|"
     r"facturado|cuesta|costo|coste|carrera)\b", "importe_total"),
]

# Un numero pegado a "viaje" es un identificador... salvo que sea el
# comienzo de una fecha ("viajes 1 de enero", "viajes 31/12").
_NO_FECHA = rf"(?!\s*(?:de\s+)?(?:{_RE_MES})\b)(?![/.-]\d)"
_RE_VIAJE = re.compile(
    rf"\b(?:viajes?|trayectos?|carreras?|servicios?)\s*(?:n(?:umero|o|º|°)?\.?\s*)?#?\s*(\d{{1,9}})\b{_NO_FECHA}"
    rf"|#\s*(\d{{1,9}})\b"
)


def extraer_borough(t: str) -> str | None:
    for patron, nombre in BOROUGHS:
        if _hay(patron, t):
            return nombre
    return None


def extraer_viaje(t: str) -> tuple[int | None, tuple[int, int] | None]:
    """Numero de viaje mencionado y su posicion en el texto."""
    m = _RE_VIAJE.search(t)
    if not m:
        return None, None
    grupo = 1 if m.group(1) else 2
    return int(m.group(grupo)), m.span()


def extraer_fecha(t: str) -> tuple[tuple[int | None, int, int] | None, tuple[int, int] | None]:
    """
    Devuelve (anio o None, mes, dia) y la posicion de la expresion.
    Formatos: 2020-01-01 · 01/01/2020 · 1 de enero (de 2020) · nochevieja.
    """
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))), m.span()
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b", t)
    if m:
        anio = int(m.group(3))
        anio = anio + 2000 if anio < 100 else anio
        return (anio, int(m.group(2)), int(m.group(1))), m.span()
    m = re.search(
        rf"\b(\d{{1,2}})\s*(?:de\s+)?({_RE_MES})(?:\s+(?:de|del)\s+(\d{{4}}))?\b", t
    )
    if m:
        anio = int(m.group(3)) if m.group(3) else None
        return (anio, MESES[m.group(2)], int(m.group(1))), m.span()
    m = re.search(r"\bnochevieja\b", t)
    if m:
        return (None, 12, 31), m.span()
    m = re.search(r"\b(dia de )?ano nuevo\b", t)
    if m:
        return (None, 1, 1), m.span()
    return None, None


def resolver_fecha(partes: tuple[int | None, int, int],
                   disponibles: list[date]) -> date | None:
    """
    Convierte (anio?, mes, dia) en fecha. Si falta el anio, se elige el
    del ultimo dia con datos que coincida: "1 de enero" en un dataset de
    2020 es el 1 de enero de 2020, no el del anio en curso.
    """
    anio, mes, dia = partes
    try:
        if anio:
            return date(anio, mes, dia)
        coincidencias = [d for d in disponibles if d.month == mes and d.day == dia]
        if coincidencias:
            return max(coincidencias)
        anio = max(d.year for d in disponibles) if disponibles else date.today().year
        return date(anio, mes, dia)
    except ValueError:
        return None


def extraer_campo(t: str) -> str | None:
    for patron, campo in CAMPOS:
        if _hay(patron, t):
            return campo
    return None


_NUM = r"-?\d+(?:[.,]\d{1,2})?"


def _a_float(texto: str) -> float:
    return float(texto.replace(",", "."))


def extraer_valor(t: str, excluir: list[tuple[int, int] | None]) -> float | None:
    """
    Valor numerico de una correccion. Se ignoran el numero de viaje y la
    fecha, y se prefiere el numero que sigue a un conector ("eran 23,50",
    "a 5", "debería ser 2"). Acepta coma decimal.
    """
    tapado = list(t)
    for span in excluir:
        if span:
            for i in range(*span):
                tapado[i] = " "
    limpio = "".join(tapado)
    conectores = re.findall(
        rf"(?:\beran?\b|\bson\b|\bes\b|\ba\b|\bpor\b|=|:|\bser\b|\bvalor\b|\bvale\b|"
        rf"\bcorrecto\b|\bpon(?:er|le)?\b|\breal\b|\bnuevo\b)\s*(?:de\s+)?\$?\s*({_NUM})",
        limpio,
    )
    if conectores:
        return _a_float(conectores[-1])
    numeros = re.findall(rf"(?<![\w.,]){_NUM}(?![\w])", limpio)
    if numeros:
        return _a_float(numeros[-1])
    return None


def extraer_motivo(original: str, t: str, span_viaje: tuple[int, int] | None) -> str | None:
    """Motivo explicito: 'porque ...', 'motivo: ...' o lo que sigue a 'viaje N,'."""
    m = re.search(r"\b(?:motivo\s*:?|porque|ya que|debido a|pues)\s+(.{3,})$", t)
    if m:
        return original[m.start(1):].strip(" .")
    if span_viaje:
        resto = t[span_viaje[1]:]
        m = re.match(r"\s*[,;:.\-]\s*(.{3,})$", resto)
        if m:
            inicio = span_viaje[1] + m.start(1)
            candidato = original[inicio:].strip(" .")
            # Si lo que sigue es la propia correccion ("importe 23,50") no es motivo
            if not re.fullmatch(rf".*?{_NUM}\s*\$?", normalizar(candidato)):
                return candidato
    return None


# ---------------------------------------------------------------------
# Intenciones
# ---------------------------------------------------------------------

RE_SALUDO = r"^\W*(hola|buenas|buenos dias|buenas tardes|buenas noches|hey|saludos|hi|hello)\b"
RE_AYUDA = (r"\b(ayuda|ayudame|que puedes hacer|que sabes hacer|que puedo preguntar|"
            r"que te puedo preguntar|como funciona|como te uso|help|opciones)\b")
RE_GRACIAS = r"^\W*(muchas )?(gracias|genial|perfecto|estupendo)\W*$"
RE_AFIRMA = (r"^\W*(si|s|vale|ok|okay|confirmo|confirmado|confirmar|adelante|hazlo|correcto|"
             r"de acuerdo|claro|dale|yes|afirmativo)\b")
RE_NIEGA = r"^\W*(no|nop|negativo|olvidalo|dejalo|mejor no|para|espera|cancela(r)?( eso)?)\W*$"

RE_CANCELAR = (r"\b(cancela|cancelar|cancelalo|cancelala|anula|anular|anulalo|anulala|"
               r"da de baja|dar de baja)\b")
RE_CORREGIR = (r"\b(corrige|corregir|corrigelo|corrigela|corrija|cambia|cambiar|cambiale|"
               r"modifica|modificar|actualiza|actualizar|ajusta|ajustar|rectifica|rectificar|"
               r"tenia mal|estaba mal|esta mal|deberia ser|pon|ponle|poner)\b")
RE_EXPLICAR = (r"\bpor ?que\b|\b(ha|han|habia) (cambiado|variado|bajado|subido)\b|\bhistorial\b|"
               r"\bcorrecciones\b|\bcorregid\w*|\bcancelad\w*|\bcancelaciones\b|"
               r"\btrazabilidad\b|\bcambios\b|\bquien (ha )?(cambiado|corregido)\b")
RE_GLOBAL = (r"\b(global(es)?|todas las empresas|entre empresas|toda la plataforma|"
             r"plataforma entera|de la plataforma|ambas empresas|las dos empresas|"
             r"cada empresa|otras empresas)\b")
RE_ZONAS = (r"\b(zonas?|barrios?|de donde salen|donde se recogen|donde se cogen|"
            r"puntos? de recogida|origen(es)?|donde hay mas)\b")
RE_DISTRITOS = r"\b(distritos?|boroughs?|reparto geografico|por distrito)\b"
RE_HORAS = (r"\b(horas?|horari\w*|franjas?|hora punta|momento del dia|a que hora|"
            r"cuando hay mas)\b")
RE_PAGOS = (r"\b(pagos?|pagan|pagaron|tarjetas?|efectivo|propinas?|metodos? de pago|"
            r"formas? de pago)\b")
RE_LISTAR = (r"\b(lista\w*|ultimos viajes|viajes recientes|ver (los )?viajes|que viajes|"
             r"muestrame (los )?viajes|ensename (los )?viajes)\b")
RE_RESUMEN = (r"\b(viajes?|cuant\w*|factur\w*|importes?|tarifas?|medi[ao]s?|total(es)?|"
              r"metricas?|resumen|ingresos?|distancias?|dinero|recaud\w*|ganado|ganamos|"
              r"kpis?|cifras?|numeros|datos|estadisticas?|carreras|actividad)\b")

INTENCIONES_FILTRABLES = ("resumen_metricas", "metricas_por_zona")


@dataclass
class Interpretacion:
    """Resultado del NLU para un mensaje."""
    texto: str                       # normalizado
    original: str
    intencion: str | None = None
    viaje_id: int | None = None
    borough: str | None = None
    fecha_partes: tuple | None = None
    campo: str | None = None
    valor: float | None = None
    motivo: str | None = None
    argumentos: dict = field(default_factory=dict)


def interpretar(mensaje: str) -> Interpretacion:
    original = unicodedata.normalize("NFC", mensaje.strip())
    t = normalizar(original)
    it = Interpretacion(texto=t, original=original)

    it.viaje_id, span_viaje = extraer_viaje(t)
    it.fecha_partes, span_fecha = extraer_fecha(t)
    it.borough = extraer_borough(t)
    it.campo = extraer_campo(t)
    it.valor = extraer_valor(t, [span_viaje, span_fecha])
    it.motivo = extraer_motivo(original, t, span_viaje)
    it.intencion = detectar_intencion(it)
    return it


def detectar_intencion(it: Interpretacion) -> str | None:
    t = it.texto
    pregunta_por_que = _hay(r"\bpor ?que\b", t) and not _hay(r"\bporque\b", t)

    # Escrituras (E6): solo con un viaje concreto y un verbo de accion.
    if it.viaje_id is not None and not pregunta_por_que:
        if _hay(RE_CANCELAR, t):
            return "cancelar_viaje"
        if _hay(RE_CORREGIR, t) and (it.campo or it.valor is not None):
            return "registrar_correccion"
    elif _hay(RE_CORREGIR, t) and it.campo and not pregunta_por_que:
        # "corrige la tarifa a 20" sin decir que viaje: se pedira
        return "registrar_correccion"
    elif _hay(RE_CANCELAR, t) and _hay(r"\bviaje\b", t) and not pregunta_por_que:
        return "cancelar_viaje"

    if it.viaje_id is not None:
        return "detalle_viaje"
    if _hay(RE_EXPLICAR, t) or _hay(r"incluye.*(corregid|correccion)", t):
        return "historial_correcciones"
    if _hay(RE_GLOBAL, t) or (_hay(r"\bcompar\w*", t) and _hay(r"\bempresas?\b", t)):
        return "metricas_globales"
    if _hay(RE_ZONAS, t):
        return "metricas_por_zona"
    if _hay(RE_DISTRITOS, t):
        return "metricas_por_borough"
    if _hay(RE_HORAS, t):
        return "viajes_por_hora"
    if _hay(RE_PAGOS, t):
        return "reparto_pagos"
    if _hay(RE_LISTAR, t):
        return "listar_viajes"
    if _hay(RE_RESUMEN, t):
        return "resumen_metricas"
    if _hay(RE_AYUDA, t):
        return "ayuda"
    if _hay(RE_SALUDO, t) and len(t.split()) <= 4:
        return "saludo"
    if _hay(RE_GRACIAS, t):
        return "gracias"
    return None


def es_afirmacion(t: str) -> bool:
    return _hay(RE_AFIRMA, t)


def es_negacion(t: str) -> bool:
    return _hay(RE_NIEGA, t)


def es_seguimiento(t: str) -> bool:
    """'¿y en Brooklyn?', 'y el 31 de diciembre', 'ahora en Queens'."""
    return _hay(r"^\W*(y|e|ahora|tambien|igual|lo mismo|y si)\b", t)


# ---------------------------------------------------------------------
# Conversion al contrato comun (esquema_nlu), para comparar este NLU con
# el modelo de lenguaje y para que el motor trate igual a los dos.
# ---------------------------------------------------------------------

def a_marco(it: Interpretacion) -> dict:
    """Interpretacion de las reglas -> JSON del esquema_nlu."""
    from app import esquema_nlu as E

    t = it.texto
    intencion = it.intencion
    if intencion is None:
        if es_afirmacion(t) and len(t.split()) <= 4:
            intencion = "afirmar"
        elif es_negacion(t):
            intencion = "negar"
        elif it.borough or it.fecha_partes:
            intencion = "seguimiento"
        else:
            intencion = "fuera_de_dominio"
    elif intencion == "resumen_metricas" and es_seguimiento(t) and (it.borough or it.fecha_partes) \
            and not _hay(r"\b(viajes?|factur\w*|importes?|tarifas?)\b", t):
        intencion = "seguimiento"
    marco = E.vacio(intencion)
    marco.update({
        "viaje_id": it.viaje_id,
        "campo": it.campo if intencion == "registrar_correccion" else None,
        "valor": it.valor if intencion == "registrar_correccion" else None,
        "borough": it.borough,
        "fecha": E.partes_a_fecha(it.fecha_partes),
        "motivo": it.motivo if intencion in ("registrar_correccion", "cancelar_viaje") else None,
    })
    return marco


def desde_marco(marco: dict, mensaje: str) -> Interpretacion:
    """JSON del esquema_nlu (p. ej. salida del modelo) -> Interpretacion
    que entiende el motor de dialogo."""
    from app import esquema_nlu as E

    original = unicodedata.normalize("NFC", mensaje.strip())
    it = Interpretacion(texto=normalizar(original), original=original)
    intencion = marco["intencion"]
    # Las intenciones de conversacion del modelo se traducen a lo que el
    # motor ya sabe manejar (None = "sin intencion propia").
    it.intencion = None if intencion in ("seguimiento", "fuera_de_dominio", "afirmar", "negar") \
        else intencion
    it.viaje_id = marco.get("viaje_id")
    it.campo = marco.get("campo")
    it.valor = marco.get("valor")
    it.borough = marco.get("borough")
    it.fecha_partes = E.fecha_a_partes(marco.get("fecha"))
    it.motivo = marco.get("motivo")
    it.argumentos = {"_intencion_modelo": intencion}
    return it
