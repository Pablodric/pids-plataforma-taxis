"""
Genera el dataset de fine-tuning del NLU (mensaje en español -> JSON).

    python llm/generar_dataset.py            # escribe llm/datos/*.jsonl

Decisiones:
  - Plantillas escritas a mano por intencion, con huecos para las entidades
    (viaje, campo, valor, distrito, fecha, motivo). Los valores salen de los
    datos reales cuando tiene sentido (ids de viaje, fechas del CSV).
  - Las plantillas de TEST son distintas de las de entrenamiento. Asi la
    evaluacion mide si el modelo generaliza a frases que nunca ha visto, no
    si memoriza las de entrenamiento.
  - Ruido realista: sin tildes, minusculas, sin signos de apertura, alguna
    errata y muletillas ("oye", "porfa"). La etiqueta del motivo se calcula
    DESPUES del ruido, para que sea literalmente el texto del mensaje.
  - Formato de salida: conversaciones {system, user, assistant} en JSONL, el
    formato que entienden TRL, Unsloth, Axolotl y el script entrenar.py.
"""

import argparse
import csv
import json
import random
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))
from app import esquema_nlu as E  # noqa: E402

DATOS_CSV = RAIZ / "ingest" / "datos" / "rows.csv"
SALIDA = Path(__file__).resolve().parent / "datos"

# ---------------------------------------------------------------------
# Valores de las entidades
# ---------------------------------------------------------------------

BOROUGH_TXT = {
    "Manhattan": ["Manhattan", "manhattan"],
    "Brooklyn": ["Brooklyn", "brooklyn"],
    "Queens": ["Queens", "queens"],
    "Bronx": ["el Bronx", "Bronx", "the Bronx"],
    "Staten Island": ["Staten Island", "Staten"],
    "EWR": ["Newark", "el aeropuerto de Newark", "EWR"],
}

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]

CAMPO_TXT = {
    "importe_total": ["la tarifa", "el importe", "el precio", "el total", "el importe total",
                      "lo cobrado", "el cobro"],
    "propina": ["la propina"],
    "distancia": ["la distancia", "las millas", "el recorrido", "la distancia recorrida"],
    "pasajeros": ["los pasajeros", "el número de pasajeros", "las personas"],
}
CAMPO_NUDO = {  # sin articulo, para "importe 23,50"
    "importe_total": ["importe", "tarifa", "precio", "total"],
    "propina": ["propina"],
    "distancia": ["distancia", "millas"],
    "pasajeros": ["pasajeros"],
}

NUMEROS_TXT = {1: "uno", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco", 6: "seis"}

MOTIVOS = [
    "el taxímetro falló", "error del conductor al teclear", "el proveedor mandó un dato erróneo",
    "el cliente pagó menos", "faltaba el peaje", "se cobró dos veces", "la propina era en efectivo",
    "el GPS marcó mal la ruta", "reclamación del cliente", "revisión mensual del proveedor",
    "el cliente anuló el servicio", "viaje duplicado", "prueba del sistema",
    "el conductor no llegó a recogerlo", "el pasajero no se presentó", "servicio no realizado",
    "fallo de la aplicación", "tarifa aeroportuaria mal aplicada", "error de redondeo",
    "lo confirmó el proveedor por correo", "auditoría interna",
]

FUERA = [
    "qué tiempo hace hoy", "cuéntame un chiste", "quién ganó el mundial", "recomiéndame una película",
    "cómo se hace una tortilla", "cuál es la capital de Francia", "escribe un poema",
    "qué hora es en Tokio", "traduce hello al español", "cuánto es 2 más 2",
    "dame una receta de paella", "quién eres", "me aburro", "pon música",
]


def cargar_fechas_reales() -> list[str]:
    fechas = set()
    with open(DATOS_CSV, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            try:
                d = datetime.strptime(fila["tpep_pickup_datetime"], "%m/%d/%Y %I:%M:%S %p")
                fechas.add(d.date().isoformat())
            except (KeyError, ValueError):
                pass
    return sorted(fechas)


def n_viajes() -> int:
    with open(DATOS_CSV, encoding="utf-8") as f:
        return sum(1 for _ in f) - 1


class Generador:
    def __init__(self, semilla: int):
        self.r = random.Random(semilla)
        self.fechas_reales = cargar_fechas_reales()
        self.max_viaje = n_viajes()

    # ---------- entidades ----------
    def viaje(self) -> int:
        # Sobre todo ids reales; a veces uno grande (no existe, pero el NLU
        # debe extraerlo igual: la existencia la comprueba la base de datos)
        return self.r.randint(1, self.max_viaje) if self.r.random() < .85 else self.r.randint(1000, 99999)

    def borough(self):
        b = self.r.choice(list(BOROUGH_TXT))
        return b, self.r.choice(BOROUGH_TXT[b])

    def fecha(self):
        r = self.r.random()
        if r < .45:
            iso = self.r.choice(self.fechas_reales)
        else:
            anio = self.r.choice([2019, 2020, 2020, 2021])
            iso = f"{anio}-{self.r.randint(1, 12):02d}-{self.r.randint(1, 28):02d}"
        a, m, d = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
        formas = [
            (f"el {d} de {MESES[m - 1]}", f"--{m:02d}-{d:02d}"),
            (f"el {d} de {MESES[m - 1]} de {a}", iso),
            (f"el {iso}", iso),
            (f"el {d:02d}/{m:02d}/{a}", iso),
            (f"el día {d} de {MESES[m - 1]}", f"--{m:02d}-{d:02d}"),
        ]
        if d == 1:
            formas.append((f"el primero de {MESES[m - 1]}", f"--{m:02d}-01"))
        if (m, d) == (12, 31):
            formas += [("en nochevieja", "--12-31"), ("el día de nochevieja", "--12-31")]
        if (m, d) == (1, 1):
            formas += [("el día de año nuevo", "--01-01"), ("en año nuevo", "--01-01")]
        return self.r.choice(formas)

    def valor(self, campo: str):
        r = self.r
        if campo == "pasajeros":
            n = r.randint(1, 6)
            return float(n), (NUMEROS_TXT[n] if r.random() < .3 else str(n))
        if campo == "distancia":
            v = round(r.uniform(0.3, 25), r.choice([0, 1, 2]))
        elif campo == "propina":
            v = round(r.uniform(0.5, 15), r.choice([0, 2]))
        else:
            v = round(r.uniform(4, 160), r.choice([0, 2, 2]))
        entero = float(v).is_integer()
        if entero:
            v = float(int(v))
            txt = r.choice([f"{int(v)}", f"{int(v)} dólares", f"{int(v)} $"]) if campo != "distancia" \
                else r.choice([f"{int(v)}", f"{int(v)} millas"])
        else:
            coma = f"{v:.2f}".replace(".", ",")
            punto = f"{v:.2f}"
            txt = r.choice([coma, coma, punto, f"{coma} $", f"${punto}"]) if campo != "distancia" \
                else r.choice([coma, punto, f"{coma} millas"])
        return round(v, 2), txt

    # ---------- ruido ----------
    def ruido(self, texto: str) -> str:
        r = self.r
        if r.random() < .25:
            texto = r.choice(["oye, ", "hola, ", "a ver, ", "porfa, ", "perdona, "]) + texto
        if r.random() < .15:
            texto = texto + r.choice([" por favor", " porfa", " gracias", ""])
        if r.random() < .35:
            texto = sin_tildes(texto)
        if r.random() < .35:
            texto = texto.lower()
        if r.random() < .5:
            texto = texto.replace("¿", "").replace("¡", "")
        if r.random() < .08:
            texto = errata(texto, r)
        return texto


def sin_tildes(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t)
                   if unicodedata.category(c) != "Mn" or c == "̃")  # conserva la ñ


def errata(t: str, r: random.Random) -> str:
    palabras = t.split(" ")
    candidatas = [i for i, p in enumerate(palabras) if p.isalpha() and len(p) > 4]
    if not candidatas:
        return t
    i = r.choice(candidatas)
    p = palabras[i]
    j = r.randint(1, len(p) - 2)
    palabras[i] = p[:j] + p[j + 1] + p[j] + p[j + 2:]
    return " ".join(palabras)


# ---------------------------------------------------------------------
# Plantillas: (entrenamiento, test). Huecos: {b} distrito, {f} fecha,
# {v} viaje, {c} campo con articulo, {cn} campo sin articulo, {x} valor,
# {m} motivo.
# ---------------------------------------------------------------------

P = {
    "resumen_metricas": (
        ["¿cuántos viajes tenemos?", "¿cuántos viajes hemos hecho {f}?", "resumen de métricas",
         "tarifa media en {b}", "¿cuánto facturamos {f}?", "¿cuánto hemos facturado en {b}?",
         "dame las cifras de mi empresa", "importe medio de los viajes", "¿cuál es la distancia media?",
         "¿cuántos viajes hubo en {b} {f}?", "¿qué tal vamos de facturación?", "¿cuánto se ha recaudado?",
         "métricas de {b}", "número de viajes {f}", "¿cuál es la propina media?",
         "total facturado", "kpis de la empresa", "¿cuánto dinero hemos ganado {f}?"],
        ["¿qué volumen de viajes llevamos?", "necesito saber lo que ingresamos {f}",
         "precio medio de una carrera en {b}", "¿cuántas carreras se hicieron en {b}?",
         "estadísticas generales", "¿y la facturación de {f}?"],
    ),
    "metricas_por_zona": (
        ["zonas con más viajes", "¿en qué zonas se recogen más viajes?", "top zonas de recogida",
         "¿qué barrios tienen más demanda?", "zonas con más viajes en {b}", "ranking de zonas {f}",
         "¿de dónde salen más taxis?", "principales puntos de recogida en {b}",
         "¿cuáles son las zonas más populares?"],
        ["¿dónde recogemos más clientes?", "barrios más activos de {b}",
         "lista de zonas ordenada por viajes", "¿qué zonas de origen dominan {f}?"],
    ),
    "metricas_por_borough": (
        ["reparto por distrito", "viajes por distrito", "¿cómo se reparten los viajes entre distritos?",
         "desglose por borough", "facturación por distrito", "distribución geográfica de los viajes"],
        ["compárame los distritos", "¿qué distrito concentra más viajes?", "viajes agrupados por borough"],
    ),
    "viajes_por_hora": (
        ["¿a qué hora hay más demanda?", "hora punta", "distribución horaria de los viajes",
         "viajes por hora", "¿en qué franja horaria trabajamos más?", "¿cuándo hay más viajes?"],
        ["¿cuál es el momento del día con más carreras?", "demanda según la hora",
         "¿a qué horas se concentran los servicios?"],
    ),
    "reparto_pagos": (
        ["¿cómo pagan nuestros clientes?", "métodos de pago", "¿cuántos pagan con tarjeta?",
         "reparto entre efectivo y tarjeta", "¿dónde hay más propina?", "propina por método de pago",
         "formas de pago más usadas"],
        ["¿se paga más en efectivo o con tarjeta?", "desglose por medio de pago",
         "¿qué método de pago deja más propina?"],
    ),
    "listar_viajes": (
        ["lista los últimos viajes", "muéstrame los últimos viajes", "ver viajes recientes",
         "¿qué viajes tenemos?", "enséñame los viajes", "listado de viajes"],
        ["dame los viajes más recientes", "quiero ver los últimos servicios registrados"],
    ),
    "detalle_viaje": (
        ["detalle del viaje {v}", "¿qué pasó con el viaje {v}?", "muéstrame el viaje {v}",
         "ficha del viaje {v}", "información del viaje número {v}", "¿por qué ha cambiado el viaje {v}?",
         "datos del viaje #{v}", "enséñame el viaje nº {v}", "historial del viaje {v}"],
        ["quiero ver el servicio {v}", "¿qué cambios tiene el viaje {v}?", "consulta el trayecto {v}",
         "¿cómo está el viaje {v}?"],
    ),
    "historial_correcciones": (
        ["¿por qué ha cambiado esa cifra?", "¿esto incluye datos corregidos?", "historial de correcciones",
         "¿qué correcciones se han hecho?", "¿por qué ha variado la facturación?",
         "¿quién ha corregido datos?",
         "¿hay viajes cancelados?", "últimos cambios en los datos", "¿por qué ha bajado el total?",
         "muéstrame las cancelaciones"],
        ["¿a qué se debe la diferencia en las cifras?", "¿se ha modificado algún dato?",
         "registro de cambios", "¿por qué no me cuadra el total de ayer?"],
    ),
    "metricas_globales": (
        ["dame las métricas globales", "compara las dos empresas", "métricas de toda la plataforma",
         "¿cuántos viajes hay en todas las empresas?", "comparativa entre empresas",
         "datos globales de la plataforma", "¿qué empresa factura más?"],
        ["resumen de todas las compañías", "ponme las empresas una al lado de otra",
         "totales de la plataforma entera"],
    ),
    "registrar_correccion": (
        ["corrige el viaje {v}, {cn} {x}", "el viaje {v} tenía mal {c}, eran {x}",
         "cambia {c} del viaje {v} a {x}", "en el viaje {v} {c} debería ser {x}",
         "corrige {c} del viaje {v}: {x}", "el viaje {v} tiene mal {c}, el correcto es {x}",
         "actualiza {c} del viaje {v} a {x} porque {m}", "modifica el viaje {v}, {cn} {x}, motivo: {m}",
         "el viaje {v} estaba mal: {c} era {x}", "pon {c} del viaje {v} a {x}",
         "corrige el viaje {v}, {cn} {x}, {m}", "ajusta {c} del viaje {v} a {x}",
         "corrige {c} del viaje {v}", "quiero corregir el viaje {v}"],
        ["en el viaje {v} {c} correcto son {x}", "rectifica el trayecto {v}: {cn} {x} ya que {m}",
         "hay que cambiar {c} del servicio {v}, ponle {x}", "el viaje {v} se registró con {c} mal, es {x}",
         "necesito arreglar {c} del viaje {v}"],
    ),
    "cancelar_viaje": (
        ["cancela el viaje {v}", "cancela el viaje {v}, {m}", "anula el viaje {v}",
         "cancela el viaje {v} porque {m}", "hay que cancelar el viaje {v}, motivo: {m}",
         "da de baja el viaje {v}", "anula el viaje número {v}, {m}"],
        ["el viaje {v} no se hizo, cancélalo", "quiero anular el servicio {v} porque {m}",
         "cancelar trayecto {v}, {m}"],
    ),
    "saludo": (
        ["hola", "buenas", "buenos días", "hola, ¿qué tal?", "hey", "buenas tardes"],
        ["saludos", "holaa", "muy buenas"],
    ),
    "ayuda": (
        ["ayuda", "¿qué puedes hacer?", "¿qué te puedo preguntar?", "¿cómo funciona esto?",
         "¿para qué sirves?", "opciones"],
        ["no sé qué preguntarte", "¿qué sabes hacer?", "¿me explicas qué haces?"],
    ),
    "gracias": (
        ["gracias", "muchas gracias", "perfecto, gracias", "genial"],
        ["mil gracias", "te lo agradezco"],
    ),
    "afirmar": (
        ["sí", "si", "vale", "confirmo", "adelante", "sí, hazlo", "correcto", "de acuerdo", "ok"],
        ["sí, confirmado", "dale", "claro que sí"],
    ),
    "negar": (
        ["no", "no, déjalo", "mejor no", "olvídalo", "cancela eso", "no lo hagas"],
        ["nop", "espera, no", "no, gracias"],
    ),
    "seguimiento": (
        ["¿y en {b}?", "y {f}", "¿y {f}?", "¿y en {b} {f}?", "ahora en {b}", "lo mismo en {b}",
         "¿y para {b}?", "y en {b}"],
        ["¿qué tal en {b}?", "igual pero {f}", "repite para {b}"],
    ),
    "fuera_de_dominio": (FUERA[:10], FUERA[10:]),
}

CUANTOS = {  # ejemplos de entrenamiento por intencion; test = 1/5
    "registrar_correccion": 450, "cancelar_viaje": 250, "resumen_metricas": 320,
    "metricas_por_zona": 200, "detalle_viaje": 180, "historial_correcciones": 160,
    "seguimiento": 200, "metricas_por_borough": 110, "viajes_por_hora": 110,
    "reparto_pagos": 110, "listar_viajes": 90, "metricas_globales": 120,
    "saludo": 60, "ayuda": 80, "gracias": 50, "afirmar": 90, "negar": 80,
    "fuera_de_dominio": 110,
}


def rellenar(g: Generador, intencion: str, plantilla: str) -> tuple[str, dict]:
    marco = E.vacio(intencion)
    campo = g.r.choice(E.CAMPOS)
    huecos = {}
    motivo_txt = None
    if "{b}" in plantilla:
        marco["borough"], huecos["b"] = g.borough()
    if "{f}" in plantilla:
        huecos["f"], marco["fecha"] = g.fecha()
    if "{v}" in plantilla:
        marco["viaje_id"] = g.viaje()
        huecos["v"] = str(marco["viaje_id"])
    if "{c}" in plantilla or "{cn}" in plantilla:
        marco["campo"] = campo
        huecos["c"] = g.r.choice(CAMPO_TXT[campo])
        huecos["cn"] = g.r.choice(CAMPO_NUDO[campo])
    if "{x}" in plantilla:
        marco["valor"], huecos["x"] = g.valor(campo)
    if "{m}" in plantilla:
        motivo_txt = g.r.choice(MOTIVOS)
        huecos["m"] = "\x00MOTIVO\x00"
    texto = plantilla.format(**huecos)
    # El "pasajeros" en palabras solo encaja sin "pasajeros" delante duplicado
    texto = g.ruido(texto)
    if motivo_txt:
        # El ruido se aplica tambien al motivo, y la etiqueta es el texto final
        motivo_final = g.ruido_motivo(motivo_txt, texto)
        texto = texto.replace("\x00MOTIVO\x00".lower(), motivo_final).replace("\x00MOTIVO\x00", motivo_final)
        marco["motivo"] = motivo_final
    return texto, marco


def _ruido_motivo(self, motivo: str, texto: str) -> str:
    # Si el mensaje quedo en minusculas o sin tildes, el motivo tambien
    sin = texto == sin_tildes(texto)
    if sin:
        motivo = sin_tildes(motivo)
    if texto == texto.lower():
        motivo = motivo.lower()
    return motivo


Generador.ruido_motivo = _ruido_motivo


def generar(semilla: int, factor: float) -> dict[str, list[dict]]:
    g = Generador(semilla)
    conjuntos = {"train": [], "val": [], "test": []}
    for intencion, (tr, te) in P.items():
        n = max(10, int(CUANTOS[intencion] * factor))
        vistos = set()
        for _ in range(n * 3):
            if len([e for e in conjuntos["train"] if e["intencion"] == intencion]) >= n:
                break
            texto, marco = rellenar(g, intencion, g.r.choice(tr))
            if texto in vistos:
                continue
            vistos.add(texto)
            destino = "val" if g.r.random() < .1 else "train"
            conjuntos[destino].append({"intencion": intencion, "texto": texto, "marco": marco})
        vistos_te = set()
        for _ in range(max(12, n // 5) * 4):
            if len(vistos_te) >= max(12, n // 5):
                break
            texto, marco = rellenar(g, intencion, g.r.choice(te))
            if texto in vistos_te or texto in vistos:
                continue
            vistos_te.add(texto)
            conjuntos["test"].append({"intencion": intencion, "texto": texto, "marco": marco})
    for c in conjuntos.values():
        g.r.shuffle(c)
    return conjuntos


def a_chat(ejemplo: dict) -> dict:
    return {"messages": [
        {"role": "system", "content": E.PROMPT_SISTEMA},
        {"role": "user", "content": ejemplo["texto"]},
        {"role": "assistant", "content": E.serializar(ejemplo["marco"])},
    ]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--semilla", type=int, default=42)
    ap.add_argument("--factor", type=float, default=1.0, help="multiplica el tamaño del dataset")
    ap.add_argument("--salida", type=Path, default=SALIDA)
    a = ap.parse_args()

    conjuntos = generar(a.semilla, a.factor)
    a.salida.mkdir(parents=True, exist_ok=True)
    for nombre, ejemplos in conjuntos.items():
        with open(a.salida / f"{nombre}.jsonl", "w", encoding="utf-8") as f:
            for e in ejemplos:
                f.write(json.dumps(a_chat(e), ensure_ascii=False) + "\n")
    # Test tambien en formato plano, comodo para evaluar.py
    with open(a.salida / "test_etiquetado.jsonl", "w", encoding="utf-8") as f:
        for e in conjuntos["test"]:
            f.write(json.dumps({"texto": e["texto"], "esperado": e["marco"]}, ensure_ascii=False) + "\n")

    for nombre, ejemplos in conjuntos.items():
        print(f"{nombre:5s}: {len(ejemplos):5d} ejemplos")
    print("por intención (train):", dict(Counter(e["intencion"] for e in conjuntos["train"]).most_common()))
    print(f"escrito en {a.salida}")


if __name__ == "__main__":
    main()
