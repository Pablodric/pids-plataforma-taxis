"""
Genera docs/flujo_chatbot.svg y docs/flujo_chatbot.png: el recorrido de un
mensaje por el chatbot, paso a paso, tal y como lo implementa api/app/motor.py.

    python docs/diagramas/flujo_chatbot.py
"""

import sys
from pathlib import Path
from xml.sax.saxutils import escape

DOCS = Path(__file__).resolve().parents[1]
ANCHO, ALTO = 1800, 2200

C = {
    "fondo": "#F7F6F3", "tinta": "#1C2430", "suave": "#5A6472", "linea": "#D9D5CB",
    "tarjeta": "#FFFFFF", "e6": "#B8860B", "e6f": "#FFF3D1", "e7": "#1A5FA8", "e7f": "#E3EEFA",
    "ia": "#6B3FA0", "iaf": "#F0E8FA", "ok": "#1D7A54", "okf": "#E2F4EA", "mal": "#B3261E",
    "malf": "#FCEBEA", "dec": "#FFFBEF", "decb": "#C9A227",
}
SANS = "Liberation Sans, Arial, Helvetica, sans-serif"
partes: list[str] = []
defs: list[str] = []


def t(x, y, texto, tam=13, peso=400, color=None, anclaje="start"):
    partes.append(f'<text x="{x}" y="{y}" font-family="{SANS}" font-size="{tam}" font-weight="{peso}" '
                  f'fill="{color or C["tinta"]}" text-anchor="{anclaje}">{escape(texto)}</text>')


def rect(x, y, w, h, fondo, borde, radio=10, grosor=1.5, discontinua=False):
    g = ' stroke-dasharray="7 5"' if discontinua else ""
    partes.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radio}" fill="{fondo}" '
                  f'stroke="{borde}" stroke-width="{grosor}"{g}/>')


def etiqueta(x, y, texto, color, fondo):
    w = 9 + len(texto) * 7.2
    partes.append(f'<rect x="{x}" y="{y - 13}" width="{w}" height="18" rx="9" fill="{fondo}" '
                  f'stroke="{color}" stroke-width="1"/>')
    t(x + w / 2, y + 0.5, texto, 11, 700, color, "middle")
    return w


def caja(x, y, w, titulo, lineas=(), tipo="proceso", marcas=(), fin=False):
    """Caja de proceso. tipo: proceso | respuesta | error | ia. Devuelve (x, y, w, h)."""
    fondo, borde = {"proceso": (C["tarjeta"], C["tinta"]), "respuesta": (C["okf"], C["ok"]),
                    "error": (C["malf"], C["mal"]), "ia": (C["iaf"], C["ia"])}[tipo]
    h = 32 + 18 * len(lineas)
    rect(x, y, w, h, fondo, borde, radio=10 if tipo == "proceso" else 16)
    t(x + 14, y + 22, titulo, 14, 700)
    px = x + w - 10
    for m in reversed(marcas):
        px -= 9 + len(m[0]) * 7.2
        etiqueta(px, y + 18, *m)
        px -= 5
    for i, linea in enumerate(lineas):
        t(x + 14, y + 42 + 18 * i, linea, 12.5, 400, C["suave"])
    if fin:  # termina el turno: la respuesta sale por el paso 12
        etiqueta(x + w - 88, y + h - 2, "→ respuesta", C["ok"], C["tarjeta"])
    return x, y, w, h


def rombo(cx, cy, w, h, lineas):
    partes.append(f'<polygon points="{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} '
                  f'{cx - w / 2},{cy}" fill="{C["dec"]}" stroke="{C["decb"]}" stroke-width="1.8"/>')
    base = cy - (len(lineas) - 1) * 9 + 5
    for i, linea in enumerate(lineas):
        t(cx, base + 18 * i, linea, 13.5, 700, C["tinta"], "middle")


def punta(color):
    idp = f"p{color[1:]}"
    if not any(idp in d for d in defs):
        defs.append(f'<marker id="{idp}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
                    f'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
                    f'fill="{color}"/></marker>')
    return idp


def linea(puntos, color=None, texto=None, tx=None, ty=None, anclaje="start", discontinua=False):
    color = color or C["tinta"]
    g = ' stroke-dasharray="6 5"' if discontinua else ""
    pts = " ".join(f"{x},{y}" for x, y in puntos)
    partes.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"{g} '
                  f'marker-end="url(#{punta(color)})"/>')
    if texto:
        t(tx, ty, texto, 12.5, 700, color, anclaje)


def paso(n, x, y):
    partes.append(f'<circle cx="{x}" cy="{y}" r="14" fill="{C["tinta"]}"/>')
    t(x, y + 5, str(n), 13, 700, "#FFFFFF", "middle")


E6 = ("E6", C["e6"], C["e6f"])
E7 = ("E7", C["e7"], C["e7f"])
IA = ("IA", C["ia"], C["iaf"])

# ------------------------------------------------------------------ cabecera
t(40, 52, "Chatbot — flujo de una conversación", 28, 700)
t(40, 80, "Qué le pasa a cada mensaje, en el orden en que lo procesa api/app/motor.py", 15, 400,
  C["suave"])
lx = 1040
for texto, color, fondo, desc in (("E6", C["e6"], C["e6f"], "datos corregibles"),
                                  ("E7", C["e7"], C["e7f"], "multiempresa"),
                                  ("IA", C["ia"], C["iaf"], "modelo local")):
    w = etiqueta(lx, 58, texto, color, fondo)
    t(lx + w + 7, 63, desc, 13, 400, C["suave"])
    lx += w + 20 + len(desc) * 7.2
rect(1520, 45, 240, 26, C["okf"], C["ok"], radio=13)
t(1640, 63, "→ respuesta: termina el turno", 12.5, 700, C["ok"], "middle")

CX, BW = 540, 440          # eje principal
BX = CX - BW / 2
R1, R1W = 850, 400         # columna de ramas 1
R2, R2W = 1310, 450        # columna de ramas 2
IZ, IZW = 40, 250          # columna izquierda

# ------------------------------------------------------------------ 1-4 entrada
_, y, _, h = caja(BX, 110, BW, "Usuario escribe en el panel",
                  ["o pulsa los botones Sí / No de una confirmación"])
paso(1, BX - 24, 128)
linea([(CX, 110 + h), (CX, 198)])
_, y, _, h = caja(BX, 200, BW, "POST /chat · autenticación", [
    "Empresa y rol salen del JWT firmado,", "nunca del texto del mensaje"], marcas=[E7])
paso(2, BX - 24, 218)
caja(IZ, 222, IZW - 30, "401", ["Sesión caducada → login"], "error")
linea([(BX, 250), (IZ + IZW - 28, 250)], C["mal"])
linea([(CX, 200 + h), (CX, 316)])
_, y, _, h = caja(BX, 318, BW, "Cuota de la empresa", [
    "Ventana deslizante en Redis (script Lua atómico)"], marcas=[E7])
paso(3, BX - 24, 336)
caja(IZ, 330, IZW - 30, "429", ["Reintenta en N s; las otras", "empresas no se ven afectadas"], "error")
linea([(BX, 360), (IZ + IZW - 28, 360)], C["mal"])
linea([(CX, 318 + h), (CX, 416)])
_, y, _, h = caja(BX, 418, BW, "Carga la conversación (Redis)", [
    "Historial de texto · última consulta · escritura pendiente"])
paso(4, BX - 24, 436)
linea([(CX, 418 + h), (CX, 516)])

# ------------------------------------------------------------------ 5 NLU
_, _, _, h = caja(BX, 518, BW, "Interpreta el mensaje (NLU)", [
    "Ollama: pids-nlu (o el modelo base con ejemplos)",
    "Salida forzada por esquema JSON (gramática)",
    "validar(): fuera del contrato → null",
    "anclar(): viaje, valor y motivo deben estar en el texto"], "ia", marcas=[IA])
paso(5, BX - 24, 536)
nlu_fin = 518 + h
caja(R1 + 110, 560, R1W, "NLU por reglas (respaldo)", [
    "Expresiones regulares en español:", "fechas, «23,50», sinónimos, seguimiento"])
linea([(BX + BW, 600), (R1 + 108, 600)], C["ia"], "Ollama no responde,", BX + BW + 14, 572)
t(BX + BW + 14, 589, "tarda o da JSON inválido", 12.5, 700, C["ia"])
linea([(CX, nlu_fin), (CX, 668)])
_, _, _, h = caja(BX, 670, BW, "Intención + entidades", [
    "viaje · campo · valor · distrito · fecha · motivo"])
linea([(R1 + 110 + R1W / 2, 560 + 68), (R1 + 110 + R1W / 2, 695), (BX + BW + 2, 695)], C["suave"])
linea([(CX, 670 + h), (CX, 772)])

# ------------------------------------------------------------------ 6 pendiente
Y6 = 820
rombo(CX, Y6, 400, 96, ["¿Hay una corrección", "esperando confirmación?"])
paso(6, CX - 190, Y6 - 40)
rombo(R1 + R1W / 2, Y6, 260, 90, ["¿Responde", "sí o no?"])
linea([(CX + 200, Y6), (R1 + R1W / 2 - 132, Y6)], C["tinta"], "sí", CX + 212, Y6 - 8)
_, _, _, h = caja(R2, 777, R2W, "Ejecuta la escritura", [
    "INSERT en correcciones (FK compuesta + RLS)",
    "Hash SHA-256 encadenado con el eslabón anterior",
    "El disparador recalcula solo 1 cubo de métricas"], marcas=[E6], fin=True)
linea([(R1 + R1W / 2 + 130, Y6), (R2 - 2, Y6)], C["ok"], "sí", R1 + R1W / 2 + 142, Y6 - 8)
caja(R2, 706, R2W, "«De acuerdo, no he registrado nada»", [], "respuesta", fin=True)
linea([(R1 + R1W / 2, Y6 - 45), (R1 + R1W / 2, 722), (R2 - 2, 722)],
      C["mal"], "no", R1 + R1W / 2 + 10, 748)
linea([(R1 + R1W / 2, Y6 + 45), (R1 + R1W / 2, 912), (CX + 6, 912)], C["suave"],
      "otra frase: se descarta y sigue", R1 + R1W / 2 - 10, 930, "end")
linea([(CX, Y6 + 48), (CX, 948)], texto="no", tx=CX + 10, ty=Y6 + 72)

# ------------------------------------------------------------------ 7 otra empresa
Y7 = 996
rombo(CX, Y7, 400, 96, ["¿Nombra a otra empresa?", "(«dame los datos de Movilidad Sur»)"])
paso(7, CX - 190, Y7 - 40)
caja(R1, Y7 - 44, R1W + R2W + 60, "«No tengo acceso a los datos de …»", [
    "Usuario y operador: denegado, sin tocar la base de datos (el RLS lo impediría igualmente)",
    "Auditor: recibe la comparativa global de la plataforma"], "respuesta", marcas=[E7], fin=True)
linea([(CX + 200, Y7), (R1 - 2, Y7)], C["tinta"], "sí", CX + 212, Y7 - 8)
linea([(CX, Y7 + 48), (CX, 1098)], texto="no", tx=CX + 10, ty=Y7 + 72)

# ------------------------------------------------------------------ 8 pasado
Y8 = 1146
rombo(CX, Y8, 400, 96, ["¿Pregunta por el pasado?", "(«antes de la última corrección»)"])
paso(8, CX - 190, Y8 - 40)
caja(R1, Y8 - 44, R1W + R2W + 60, "Viaje en el tiempo", [
    "viajes_en(t) reconstruye las cifras justo antes de esa corrección desde el log de eventos",
    "Responde: entonces · ahora · diferencia (sin snapshots)"], "respuesta", marcas=[E6], fin=True)
linea([(CX + 200, Y8), (R1 - 2, Y8)], C["tinta"], "sí", CX + 212, Y8 - 8)
linea([(CX, Y8 + 48), (CX, 1248)], texto="no", tx=CX + 10, ty=Y8 + 72)

# ------------------------------------------------------------------ 9 seguimiento
Y9 = 1296
rombo(CX, Y9, 400, 96, ["¿Solo cambia un filtro?", "(«¿y en Brooklyn?»)"])
paso(9, CX - 190, Y9 - 40)
caja(R1, Y9 - 34, R1W, "Recupera la última consulta", [
    "del contexto y cambia solo el filtro nuevo"])
linea([(CX + 200, Y9), (R1 - 2, Y9)], C["tinta"], "sí", CX + 212, Y9 - 8)
linea([(CX, Y9 + 48), (CX, 1398)], texto="no", tx=CX - 26, ty=Y9 + 68)

# ------------------------------------------------------------------ 10 tipo
Y10 = 1446
rombo(CX, Y10, 400, 96, ["¿Qué tipo de intención?"])
paso(10, CX - 190, Y10 - 40)

# Conversación (izquierda)
caja(IZ, 1540, IZW + 20, "Conversación", [
    "saludo · ayuda · gracias ·", "no entendida", "→ ayuda según el rol, con", "un viaje real de ejemplo"],
     "respuesta", fin=True)
linea([(CX - 200, Y10), (IZ + (IZW + 20) / 2, Y10), (IZ + (IZW + 20) / 2, 1538)], C["tinta"],
      "conversación", CX - 215, Y10 - 8, "end")

# Consulta (centro)
YC = 1560
rombo(CX, YC, 330, 80, ["¿Su rol lo permite?", "(globales: solo auditor)"])
linea([(CX, Y10 + 48), (CX, YC - 42)], texto="consulta", tx=CX + 10, ty=Y10 + 76)
linea([(R1 + R1W / 2, Y9 + 16), (R1 + R1W / 2, 1374), (CX + 6, 1374)], C["suave"],
      "con la intención anterior", R1 + R1W / 2 - 10, 1366, "end")
_, _, _, h = caja(BX + 40, 1630, BW - 80, "Herramienta SQL", [
    "Conexión con set_config(empresa, rol)", "→ el RLS filtra aunque falle el código"], marcas=[E7])
linea([(CX, YC + 40), (CX, 1628)], texto="sí", tx=CX + 10, ty=YC + 62)
_, _, _, h2 = caja(BX + 40, 1740, BW - 80, "Respuesta con cifras reales", [
    "Plantilla con los datos de la base de datos", "+ aviso si incluyen correcciones"],
    "respuesta", marcas=[E6], fin=True)
linea([(CX, 1630 + h), (CX, 1738)])
caja(IZ, 1740, IZW + 20, "Denegado", ["«solo disponible para el", "rol auditor»"], "respuesta", fin=True)
linea([(CX - 165, YC), (IZ + IZW + 45, YC), (IZ + IZW + 45, 1765), (IZ + IZW + 22, 1765)], C["mal"],
      "no", CX - 178, YC - 8, "end")

# Escritura (derecha)
XE = R1 + R1W / 2 + 60
linea([(CX + 200, Y10), (XE, Y10), (XE, 1508)], C["tinta"], "corregir / cancelar", CX + 212, Y10 - 8)
YO = 1552
rombo(XE, YO, 300, 84, ["¿Rol operador?"])
caja(R2 + 90, YO - 40, R2W - 90, "Denegado", [
    "usuario: solo consulta · auditor: solo lectura"], "respuesta", marcas=[E7], fin=True)
linea([(XE + 150, YO), (R2 + 88, YO)], C["mal"], "no", XE + 160, YO - 8)
YF = 1680
rombo(XE, YF, 330, 84, ["¿Faltan viaje, campo", "o valor?"])
linea([(XE, YO + 42), (XE, YF - 44)], texto="sí", tx=XE + 10, ty=YO + 66)
caja(R2 + 90, YF - 30, R2W - 90, "Pregunta lo que falta", [
    "«¿De qué viaje se trata?»"], "respuesta", fin=True)
linea([(XE + 165, YF), (R2 + 88, YF)], C["tinta"], "sí", XE + 175, YF - 8)
_, _, _, h = caja(XE - 210, 1780, 420, "Previsualiza (sin escribir nada)", [
    "Valor actual → nuevo · rango razonable", "viaje de tu empresa y no cancelado"], marcas=[E6])
linea([(XE, YF + 42), (XE, 1778)], texto="no", tx=XE + 10, ty=YF + 66)
_, _, _, h3 = caja(XE - 210, 1890, 420, "«¿Confirmas? (sí / no)»", [
    "Guarda la escritura pendiente en el contexto", "→ botones Sí / No; el siguiente mensaje",
    "   se resuelve en el paso 6"],
    "respuesta", fin=True)
linea([(XE, 1780 + h), (XE, 1888)])
# ------------------------------------------------------------------ 11-12 salida
YS = 2040
_, _, _, h = caja(BX, YS, BW, "Guarda la conversación y responde", [
    "Historial + contexto en Redis (caduca en 1 h) · métricas Prometheus",
    "JSON: texto, herramientas usadas, NLU usado, pendiente_confirmacion"], "respuesta")
paso(11, BX - 24, YS + 18)
linea([(CX, 1740 + h2), (CX, YS - 2)], C["ok"])
t(BX + 14, YS + h + 28,
  "Todas las cajas verdes marcadas «→ respuesta» terminan aquí: una sola salida por turno.",
  12.5, 400, C["suave"])
t(ANCHO - 20, ALTO - 14, "Generado con docs/diagramas/flujo_chatbot.py", 11, 400, C["suave"], "end")

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{ANCHO}" height="{ALTO}" '
       f'viewBox="0 0 {ANCHO} {ALTO}"><defs>{"".join(defs)}</defs>'
       f'<rect width="{ANCHO}" height="{ALTO}" fill="{C["fondo"]}"/>{"".join(partes)}</svg>')
(DOCS / "flujo_chatbot.svg").write_text(svg, encoding="utf-8")
print("docs/flujo_chatbot.svg")

if "--sin-png" not in sys.argv:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        nav = p.chromium.launch()
        pag = nav.new_page(viewport={"width": ANCHO, "height": ALTO}, device_scale_factor=1.5)
        pag.set_content(f"<html><body style='margin:0'>{svg}</body></html>")
        pag.screenshot(path=str(DOCS / "flujo_chatbot.png"))
        nav.close()
    print("docs/flujo_chatbot.png")
