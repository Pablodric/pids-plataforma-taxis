"""
Prueba de carga "vecino ruidoso" (E7): una empresa bombardea la API y se
mide si la otra lo nota.

    python scripts/vecino_ruidoso.py                       # contra http://localhost:8000
    python scripts/vecino_ruidoso.py --url http://localhost:8080/api --segundos 30

Escenario:
  1. Linea base: Movilidad Sur hace consultas normales, sin nadie mas.
  2. Ataque: Taxis del Norte lanza N peticiones concurrentes sin parar
     durante unos segundos, con sus dos usuarios a la vez (la cuota es por
     EMPRESA: repartir el ataque entre usuarios no ayuda).
  3. Mientras dura, Movilidad Sur sigue con sus consultas normales.

Se espera que el atacante reciba 429 en cuanto agota su cuota, que esos
rechazos sean baratos (antes de tocar la base de datos) y que la otra
empresa no vea errores ni una subida relevante de latencia.
"""

import argparse
import asyncio
import time

import httpx

CLAVE = "demo1234"
ATACANTES = ["ana@taxisnorte.es", "luis@taxisnorte.es"]
VICTIMA = "marta@movilidadsur.es"


async def token(cli: httpx.AsyncClient, email: str) -> dict:
    r = await cli.post("/auth/login", json={"email": email, "password": CLAVE})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["token"]}


def percentil(valores, p):
    if not valores:
        return float("nan")
    v = sorted(valores)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))]


async def consultas_normales(cli, cabecera, segundos, intervalo, resultados):
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        t0 = time.perf_counter()
        r = await cli.get("/datos/panel", headers=cabecera)
        resultados.append((r.status_code, (time.perf_counter() - t0) * 1000))
        await asyncio.sleep(intervalo)


async def ataque(cli, cabeceras, segundos, concurrencia, resultados):
    fin = time.monotonic() + segundos

    async def trabajador(i):
        cab = cabeceras[i % len(cabeceras)]
        while time.monotonic() < fin:
            t0 = time.perf_counter()
            r = await cli.get("/datos/panel", headers=cab)
            resultados.append((r.status_code, (time.perf_counter() - t0) * 1000))

    await asyncio.gather(*(trabajador(i) for i in range(concurrencia)))


def resumen(nombre, resultados):
    lat = [ms for _, ms in resultados]
    ok = [ms for c, ms in resultados if c == 200]
    rechazos = [ms for c, ms in resultados if c == 429]
    otros = len(resultados) - len(ok) - len(rechazos)
    print(f"  {nombre:34s} {len(resultados):5d} pet · {len(ok):4d} ok · {len(rechazos):5d} 429 · "
          f"{otros} errores · p50 {percentil(lat, 50):6.1f} ms · p95 {percentil(lat, 95):6.1f} ms")
    return {"peticiones": len(resultados), "ok": len(ok), "rechazos_429": len(rechazos),
            "otros": otros, "p50_ms": percentil(lat, 50), "p95_ms": percentil(lat, 95),
            "p50_rechazo_ms": percentil(rechazos, 50) if rechazos else None}


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--segundos", type=float, default=20)
    ap.add_argument("--concurrencia", type=int, default=20)
    ap.add_argument("--intervalo", type=float, default=1.5,
                    help="segundos entre consultas de la empresa 'normal' (dentro de su cuota)")
    a = ap.parse_args()

    limites = httpx.Limits(max_connections=a.concurrencia + 10)
    async with httpx.AsyncClient(base_url=a.url, timeout=30, limits=limites) as cli:
        cab_victima = await token(cli, VICTIMA)
        cab_atacantes = [await token(cli, e) for e in ATACANTES]

        print(f"Línea base ({a.segundos:.0f} s): solo Movilidad Sur, una consulta cada {a.intervalo} s")
        base = []
        await consultas_normales(cli, cab_victima, a.segundos / 2, a.intervalo, base)
        r_base = resumen("Movilidad Sur (sola)", base)

        print("Esperando a que se vacíe la ventana de cuota (60 s)...")
        await asyncio.sleep(61)

        print(f"Ataque ({a.segundos:.0f} s): Taxis del Norte con {a.concurrencia} peticiones "
              f"concurrentes y 2 usuarios")
        atq, vic = [], []
        await asyncio.gather(
            ataque(cli, cab_atacantes, a.segundos, a.concurrencia, atq),
            consultas_normales(cli, cab_victima, a.segundos, a.intervalo, vic),
        )
        r_atq = resumen("Taxis del Norte (atacante)", atq)
        r_vic = resumen("Movilidad Sur (durante el ataque)", vic)

    print("\nConclusión:")
    print(f"  - El atacante consiguió {r_atq['ok']} respuestas (su cuota) y "
          f"{r_atq['rechazos_429']} rechazos 429 (latencia observada de un rechazo, p50: "
          f"{r_atq['p50_rechazo_ms'] or 0:.1f} ms; incluye la cola del propio generador de carga).")
    print(f"  - La otra empresa: {r_vic['ok']}/{r_vic['peticiones']} correctas, "
          f"p95 {r_base['p95_ms']:.1f} → {r_vic['p95_ms']:.1f} ms.")


if __name__ == "__main__":
    asyncio.run(main())
