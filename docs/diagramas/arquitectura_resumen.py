"""
Genera docs/arquitectura_resumen.png: la arquitectura en ocho cajas, con letra
grande, para diapositivas. El detalle completo esta en arquitectura.py.

    python docs/diagramas/arquitectura_resumen.py
"""

from pathlib import Path
from xml.sax.saxutils import escape

DOCS = Path(__file__).resolve().parent.parent
ANCHO, ALTO = 1600, 680
C = {"tinta": "#1C2430", "suave": "#5A6472", "linea": "#C9C5BB", "e6": "#B8860B", "e6f": "#FFF3D1",
     "e7": "#1A5FA8", "e7f": "#E3EEFA", "ia": "#6B3FA0", "iaf": "#F0E8FA", "gris": "#F4F3EF"}
FAM = "Liberation Sans, Arial, Helvetica, sans-serif"
MARCAS = {"E6": (C["e6"], C["e6f"]), "E7": (C["e7"], C["e7f"]), "IA": (C["ia"], C["iaf"])}
p: list[str] = []


def t(x, y, texto, tam=22, peso=400, color=None, anclaje="start"):
    p.append(f'<text x="{x}" y="{y}" font-family="{FAM}" font-size="{tam}" font-weight="{peso}" '
             f'fill="{color or C["tinta"]}" text-anchor="{anclaje}">{escape(texto)}</text>')


def caja(x, y, w, h, titulo, lineas, marcas=(), fondo="#FFFFFF", borde=None, discontinua=False):
    guion = ' stroke-dasharray="9 6"' if discontinua else ""
    p.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fondo}" '
             f'stroke="{borde or C["tinta"]}" stroke-width="2.5"{guion}/>')
    t(x + 20, y + 46, titulo, 32, 700)
    px = x + w - 16
    for m in reversed(marcas):
        color, f = MARCAS[m]
        px -= 46
        p.append(f'<rect x="{px}" y="{y + 18}" width="42" height="28" rx="14" fill="{f}" '
                 f'stroke="{color}" stroke-width="1.5"/>')
        t(px + 21, y + 38, m, 17, 700, color, "middle")
        px -= 4
    for i, l in enumerate(lineas):
        t(x + 20, y + 84 + 30 * i, l, 22, 400, C["suave"])


def ruta(puntos, color=None, texto=None, tx=0, ty=0, anclaje="middle"):
    color = color or C["tinta"]
    p.append(f'<polyline points="{" ".join(f"{a},{b}" for a, b in puntos)}" fill="none" '
             f'stroke="{color}" stroke-width="3" marker-end="url(#f{color[1:]})"/>')
    if texto:
        t(tx, ty, texto, 19, 400, C["suave"], anclaje)


# fila superior: el recorrido de una peticion
caja(20, 20, 230, 280, "Navegador", ["Panel y chat,", "en directo"])
caja(310, 20, 240, 280, "nginx", ["Sirve el panel", "Proxy /api", "Límite de", "ráfagas"], ["E7"])
caja(610, 20, 410, 280, "API · FastAPI", ["Token → empresa y rol", "Cuota por empresa",
                                          "Ingesta de viajes nuevos", "Motor del chatbot"], ["E6", "E7"])
caja(1090, 20, 490, 280, "PostgreSQL", ["Viajes inmutables + log de correcciones",
                                        "Cubo de métricas por disparador",
                                        "Row Level Security por empresa"], ["E6", "E7"])
# fila inferior: lo que alimenta y apoya a la API
caja(20, 420, 530, 240, "Simulador", ["Un viaje nuevo cada 3 s, enviado con",
                                      "la cuenta de proveedor de cada empresa"], ["E7"],
     borde=C["e7"])
caja(610, 420, 410, 240, "Ollama", ["Modelo local: interpreta la frase",
                                    "Si falla, responden las reglas"], ["IA"], C["iaf"], C["ia"])
caja(1090, 420, 235, 240, "Redis", ["Cuotas, chat", "y avisos"], ["E7"])
caja(1345, 420, 235, 240, "Ingesta", ["CSV histórico,", "una sola vez"], [], C["gris"], C["suave"],
     discontinua=True)

ruta([(250, 160), (306, 160)], texto="HTTP", tx=279, ty=146)
ruta([(550, 160), (606, 160)], texto="/api", tx=580, ty=146)
ruta([(1020, 130), (1086, 130)], texto="SQL", tx=1054, ty=116)
ruta([(815, 300), (815, 416)], C["ia"], "frase → intención", 828, 366, "start")
ruta([(1020, 250), (1055, 250), (1055, 360), (1207, 360), (1207, 416)])
ruta([(1462, 420), (1462, 304)], C["suave"], "carga inicial", 1474, 366, "start")
ruta([(285, 420), (285, 360), (580, 360), (580, 250), (606, 250)], C["e7"],
     "POST /ingesta/viajes", 300, 392, "start")

marcadores = "".join(
    f'<marker id="f{c[1:]}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
    f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
    for c in (C["tinta"], C["ia"], C["e7"], C["suave"]))
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{ANCHO}" height="{ALTO}" '
       f'viewBox="0 0 {ANCHO} {ALTO}"><defs>{marcadores}</defs>'
       f'<rect width="{ANCHO}" height="{ALTO}" fill="#FFFFFF"/>' + "".join(p) + "</svg>")

from playwright.sync_api import sync_playwright  # noqa: E402

with sync_playwright() as pw:
    nav = pw.chromium.launch()
    pag = nav.new_page(viewport={"width": ANCHO, "height": ALTO}, device_scale_factor=2)
    pag.set_content(f"<html><body style='margin:0'>{svg}</body></html>")
    pag.screenshot(path=str(DOCS / "arquitectura_resumen.png"))
    nav.close()
print("docs/arquitectura_resumen.png")
