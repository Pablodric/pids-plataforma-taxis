"""
Genera docs/arquitectura.svg y docs/arquitectura.png.

    python docs/diagramas/arquitectura.py          # necesita playwright para el PNG

El diagrama es codigo: si cambia la arquitectura, se edita aqui y se
regenera, en vez de retocar una imagen a mano.
"""

import sys
from pathlib import Path
from xml.sax.saxutils import escape

AQUI = Path(__file__).resolve().parent
DOCS = AQUI.parent
ANCHO, ALTO = 1830, 1110

C = {
    "fondo": "#F7F6F3", "tinta": "#1C2430", "suave": "#5A6472", "linea": "#D9D5CB",
    "tarjeta": "#FFFFFF", "e6": "#B8860B", "e6f": "#FFF3D1", "e7": "#1A5FA8", "e7f": "#E3EEFA",
    "ia": "#6B3FA0", "iaf": "#F0E8FA", "ok": "#1D7A54", "okf": "#E2F4EA", "gris": "#EEECE6",
}

partes: list[str] = []


def t(x, y, texto, tam=14, peso=400, color=None, anclaje="start", mono=False):
    fam = "Liberation Mono, DejaVu Sans Mono, monospace" if mono else \
          "Liberation Sans, Arial, Helvetica, sans-serif"
    partes.append(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{tam}" font-weight="{peso}" '
                  f'fill="{color or C["tinta"]}" text-anchor="{anclaje}">{escape(texto)}</text>')


def caja(x, y, w, h, fondo=None, borde=None, radio=12, grosor=1.5, discontinua=False):
    guion = ' stroke-dasharray="7 5"' if discontinua else ""
    partes.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radio}" '
                  f'fill="{fondo or C["tarjeta"]}" stroke="{borde or C["linea"]}" '
                  f'stroke-width="{grosor}"{guion}/>')


def etiqueta(x, y, texto, color, fondo):
    w = 9 + len(texto) * 7.2
    partes.append(f'<rect x="{x}" y="{y - 13}" width="{w}" height="18" rx="9" fill="{fondo}" '
                  f'stroke="{color}" stroke-width="1"/>')
    t(x + w / 2, y + 0.5, texto, 11, 700, color, "middle")
    return w


def bloque(x, y, w, titulo, lineas, marcas=(), fondo=None, borde=None, tam=13):
    """Sub-caja con titulo, lineas y etiquetas E6/E7/IA alineadas a la derecha."""
    h = 30 + 18 * len(lineas)
    caja(x, y, w, h, fondo or C["tarjeta"], borde, radio=8, grosor=1)
    t(x + 12, y + 21, titulo, tam + 1, 700)
    px = x + w - 10
    for texto, color, fondo_m in reversed(marcas):
        px -= 9 + len(texto) * 7.2
        etiqueta(px, y + 17, texto, color, fondo_m)
        px -= 5
    for i, l in enumerate(lineas):
        t(x + 12, y + 41 + 18 * i, l, tam - 0.5, 400, C["suave"])
    return y + h


def flecha(x1, y1, x2, y2, texto=None, color=None, dx=0, dy=-7, anclaje="middle", discontinua=False):
    color = color or C["tinta"]
    guion = ' stroke-dasharray="6 5"' if discontinua else ""
    partes.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="2"'
                  f'{guion} marker-end="url(#punta-{color[1:]})"/>')
    if texto:
        for i, linea in enumerate(texto.split("\n")):
            t((x1 + x2) / 2 + dx, (y1 + y2) / 2 + dy + 15 * i, linea, 12, 400, C["suave"], anclaje)


E6 = ("E6", C["e6"], C["e6f"])
E7 = ("E7", C["e7"], C["e7f"])
IA = ("IA", C["ia"], C["iaf"])

# ---------------------------------------------------------------- cabecera
t(40, 52, "Plataforma de datos de taxis — arquitectura", 28, 700)
t(40, 80, "E6 datos corregibles · E7 multiempresa · ingesta en tiempo real · "
          "docker compose: 8 servicios", 15, 400, C["suave"])
lx = 1180
for texto, color, fondo, desc in (("E6", C["e6"], C["e6f"], "datos corregibles"),
                                  ("E7", C["e7"], C["e7f"], "multiempresa"),
                                  ("IA", C["ia"], C["iaf"], "modelo de lenguaje")):
    w = etiqueta(lx, 58, texto, color, fondo)
    t(lx + w + 7, 63, desc, 13, 400, C["suave"])
    lx += w + 20 + len(desc) * 7.2

# ---------------------------------------------------------------- cliente
caja(40, 130, 250, 150)
t(58, 160, "Navegador", 18, 700)
for i, l in enumerate(["Panel web: chat, KPI,", "gráficas, correcciones,", "viaje en el tiempo",
                       "En directo por WebSocket"]):
    t(58, 186 + 20 * i, l, 13, 400, C["suave"])

caja(40, 340, 250, 140, C["gris"], discontinua=True)
t(58, 368, "GitHub Actions", 16, 700)
for i, l in enumerate(["ruff + 129 pruebas contra", "Postgres y Redis reales,", "cobertura ≥ 80 %,",
                       "build de imágenes Docker"]):
    t(58, 392 + 19 * i, l, 13, 400, C["suave"])

# ---------------------------------------------------------------- nginx
caja(350, 130, 270, 230, C["tarjeta"], C["e7"], grosor=2)
t(368, 160, "nginx", 18, 700)
etiqueta(560, 155, "E7", C["e7"], C["e7f"])
for i, l in enumerate(["Sirve el panel (estático)", "Proxy /api → API (mismo origen)",
                       "Deja pasar el WebSocket del panel",
                       "limit_req por token:", "20 pet/s, ráfaga 40 → 429", "Propaga X-Request-ID"]):
    t(368, 188 + 22 * i, l, 13, 400, C["suave"])
t(368, 342, ":8080", 12, 400, C["suave"], mono=True)

# ---------------------------------------------------------------- API
AX, AY, AW = 680, 110, 480
i_api = len(partes)
t(AX + 18, AY + 32, "API · FastAPI", 20, 700)
t(AX + AW - 18, AY + 32, ":8000 · sin estado", 12, 400, C["suave"], "end", mono=True)
y = AY + 50
x, w = AX + 16, AW - 32
y = bloque(x, y, w, "1 · Autenticación y permisos", [
    "JWT firmado → empresa y rol (nunca del texto del chat)",
    "Permisos explícitos: el auditor lee todo y no escribe",
    "Bloqueo del login tras 5 fallos (Redis)"], [E7]) + 10
y = bloque(x, y, w, "2 · Cuota por empresa", [
    "Ventana deslizante, script Lua atómico en Redis",
    "Rechazo auditado una vez por ventana"], [E7]) + 10
y_ing = y
y = bloque(x, y, w, "3 · Ingesta en tiempo real", [
    "POST /ingesta/viajes: la empresa sale de la credencial",
    "Cuota propia por empresa · aviso a los paneles abiertos"], [E7]) + 10
y = bloque(x, y, w, "4 · Motor de diálogo", [
    "NLU enchufable: modelo local → reglas",
    "Salida validada y anclada al texto del mensaje",
    "Confirmación sí/no antes de cualquier escritura",
    "Contexto: «¿y en Brooklyn?»"], [IA, E6]) + 10
y = bloque(x, y, w, "5 · Herramientas", [
    "Catálogo recortado según el rol · sin parámetro de empresa"], [E7]) + 10
y = bloque(x, y, w, "6 · Repositorio SQL", [
    "Abre cada transacción con set_config(empresa, rol)",
    "Viaje en el tiempo · verificación de la cadena"], [E6, E7]) + 10
y = bloque(x, y, w, "7 · Observabilidad", [
    "/metrics Prometheus · logs JSON · X-Request-ID"], []) + 10
API_FIN = y + 6
partes.insert(i_api, f'<rect x="{AX}" y="{AY}" width="{AW}" height="{API_FIN - AY}" rx="14" '
                     f'fill="#FCFBF8" stroke="{C["tinta"]}" stroke-width="2"/>')

# ---------------------------------------------------------------- Postgres
PX, PY, PW = 1250, 110, 510
i_pg = len(partes)
t(PX + 18, PY + 32, "PostgreSQL 16", 20, 700)
t(PX + PW - 18, PY + 32, "rol pids_app, sin superusuario", 12, 400, C["suave"], "end", mono=True)
y = PY + 50
x, w = PX + 16, PW - 32
y = bloque(x, y, w, "viajes", ["Hechos inmutables (UPDATE/DELETE bloqueados)"], [E6]) + 10
y = bloque(x, y, w, "correcciones", [
    "Log de eventos de solo-añadir",
    "FK compuesta (viaje, empresa) · 1 cancelación/viaje",
    "Cadena SHA-256 por empresa → detecta manipulación"], [E6, E7], C["e6f"], C["e6"]) + 10
y = bloque(x, y, w, "v_viajes_vigentes · viajes_en(t)", [
    "Estado actual y estado en cualquier instante pasado"], [E6]) + 10
y = bloque(x, y, w, "metricas_diarias", [
    "Cubo (empresa, fecha, distrito) por disparador:",
    "una corrección o un viaje nuevo recalcula 1 cubo",
    "Invariante verificado contra el recálculo completo"], [E6]) + 10
y = bloque(x, y, w, "Row Level Security", [
    "Cada empresa solo ve sus filas; el auditor, todas",
    "Solo el proveedor inserta viajes, y solo de su empresa"], [E7], C["e7f"], C["e7"]) + 10
PG_FIN = y + 6
partes.insert(i_pg, f'<rect x="{PX}" y="{PY}" width="{PW}" height="{PG_FIN - PY}" rx="14" '
                    f'fill="#FCFBF8" stroke="{C["tinta"]}" stroke-width="2"/>')

# ---------------------------------------------------------------- Redis
RY = PG_FIN + 40
caja(PX, RY, PW, 132, C["tarjeta"], C["tinta"], radio=14, grosor=2)
t(PX + 18, RY + 32, "Redis 7", 20, 700)
for i, l in enumerate(["Historial y contexto de cada conversación (con caducidad)",
                       "Contadores de cuota por empresa · intentos de login",
                       "Avisos de viaje nuevo, un canal por empresa"]):
    t(PX + 18, RY + 60 + 22 * i, l, 13, 400, C["suave"])

# ---------------------------------------------------------------- Ollama
OY = API_FIN + 60
caja(AX, OY, AW, 150, C["iaf"], C["ia"], radio=14, grosor=2)
t(AX + 18, OY + 32, "Ollama", 20, 700)
etiqueta(AX + AW - 50, OY + 27, "IA", C["ia"], C["iaf"])
for i, l in enumerate(["Qwen2.5 de 1.500 M de parámetros, en local (CPU)",
                       "Solo interpreta la frase: intención y datos",
                       "Salida forzada por esquema JSON (gramática)",
                       "Si falla o tarda → reglas, sin cortar el chat"]):
    t(AX + 18, OY + 60 + 21 * i, l, 13, 400, C["tinta"] if i == 0 else C["suave"])

# ---------------------------------------------------------------- Ingesta
IY = OY
caja(PX, IY, PW, 150, C["tarjeta"], C["linea"], radio=14)
t(PX + 18, IY + 32, "Ingesta (una vez)", 18, 700)
for i, l in enumerate(["CSV TLC 2020 (999 viajes) o sintético + 265 zonas",
                       "Empresa asignada desde el VendorID",
                       "Huella SHA-256 del fichero · informe de anomalías",
                       "Carga el cubo inicial"]):
    t(PX + 18, IY + 60 + 21 * i, l, 13, 400, C["suave"])

# ---------------------------------------------------------------- simulador
caja(40, OY, 580, 150, C["tarjeta"], C["e7"], radio=14, grosor=2)
t(58, OY + 32, "Simulador (tiempo real)", 18, 700)
etiqueta(560, OY + 27, "E7", C["e7"], C["e7f"])
for i, l in enumerate(["Genera un viaje cada 3 s a partir de viajes reales",
                       "Lo envía con la cuenta de proveedor de su empresa",
                       "Entra por la API como cualquier cliente:",
                       "nunca escribe en la base de datos"]):
    t(58, OY + 60 + 21 * i, l, 13, 400, C["suave"])

# ---------------------------------------------------------------- flechas
for color in (C["tinta"], C["e7"], C["ia"], C["suave"]):
    partes.insert(0, f'<marker id="punta-{color[1:]}" viewBox="0 0 10 10" refX="9" refY="5" '
                     f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                     f'<path d="M0,0 L10,5 L0,10 z" fill="{color}"/></marker>')
flecha(290, 205, 348, 205, "HTTP", dy=-9)
flecha(620, 240, 678, 240, "/api", dy=-9)
t(649, 262, "+ WS", 12, 400, C["suave"], "middle")
flecha(1160, 300, 1248, 300, "SQL con\ncontexto RLS", dy=-24)
flecha(1160, RY + 55, 1248, RY + 55, "cuota, chat,\navisos", dy=-24)
flecha(AX + AW / 2, API_FIN, AX + AW / 2, OY - 2, "  /api/chat + esquema JSON", C["ia"],
       anclaje="start", dx=6, dy=4)
# Ingesta -> Postgres, rodeando Redis por el margen derecho
bx = PX + PW + 22
puntos = f"{PX + PW},{IY + 75} {bx},{IY + 75} {bx},{PY + 300} {PX + PW + 2},{PY + 300}"
punta = C["suave"][1:]
partes.append(f'<polyline points="{puntos}" fill="none" stroke="{C["suave"]}" '
              f'stroke-width="2" marker-end="url(#punta-{punta})"/>')
ym = (IY + 75 + PY + 300) / 2
partes.append(f'<text x="{bx + 15}" y="{ym}" font-family="Liberation Sans, Arial, sans-serif" '
              f'font-size="12" fill="{C["suave"]}" text-anchor="middle" '
              f'transform="rotate(-90 {bx + 15} {ym})">carga inicial</text>')
# Simulador -> API (bloque de ingesta), subiendo entre nginx y la API
sx, sy = 650, y_ing + 33
partes.append(f'<polyline points="620,{OY + 50} {sx},{OY + 50} {sx},{sy} {AX - 2},{sy}" fill="none" '
              f'stroke="{C["e7"]}" stroke-width="2" marker-end="url(#punta-{C["e7"][1:]})"/>')
t(sx - 10, (OY + sy) / 2, "POST /ingesta/viajes", 12, 400, C["suave"], "end", mono=True)
t(sx - 10, (OY + sy) / 2 + 16, "un viaje cada 3 s", 12, 400, C["suave"], "end")

# ---------------------------------------------------------------- pie
ALTO = OY + 150 + 44
t(1780, ALTO - 16, "Generado con docs/diagramas/arquitectura.py", 11, 400, C["suave"], "end")

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{ANCHO}" height="{ALTO}" '
       f'viewBox="0 0 {ANCHO} {ALTO}"><defs>'
       + "".join(p for p in partes if p.startswith("<marker"))
       + f'</defs><rect width="{ANCHO}" height="{ALTO}" fill="{C["fondo"]}"/>'
       + "".join(p for p in partes if not p.startswith("<marker")) + "</svg>")
(DOCS / "arquitectura.svg").write_text(svg, encoding="utf-8")
print("docs/arquitectura.svg")

if "--sin-png" not in sys.argv:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        nav = p.chromium.launch()
        pag = nav.new_page(viewport={"width": ANCHO, "height": ALTO}, device_scale_factor=1.5)
        pag.set_content(f"<html><body style='margin:0'>{svg}</body></html>")
        pag.screenshot(path=str(DOCS / "arquitectura.png"), full_page=False)
        nav.close()
    print("docs/arquitectura.png")
