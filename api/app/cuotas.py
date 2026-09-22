"""
Limitador de consumo por empresa (E7).

Requisito: "Evitar que una empresa consuma todos los recursos disponibles
para las demas". Implementado como ventana deslizante en Redis, con la
cuota definida por empresa en la tabla 'empresas'.

La cuota se aplica POR EMPRESA, no por usuario: si una empresa tiene diez
usuarios lanzando consultas, entre todos no pueden agotar la plataforma.
"""

import time

from fastapi import HTTPException

from app import config, db

_cuotas_cache: dict[str, int] = {}


def cuota_de(empresa_id: str) -> int:
    if empresa_id not in _cuotas_cache:
        with db.conexion_sin_contexto() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT cuota_consultas_min FROM empresas WHERE id = %s",
                (empresa_id,),
            )
            fila = cur.fetchone()
        _cuotas_cache[empresa_id] = fila["cuota_consultas_min"] if fila else 20
    return _cuotas_cache[empresa_id]


def consumir(empresa_id: str, coste: int = 1) -> dict:
    """
    Registra 'coste' unidades de consumo. Lanza 429 si la empresa se pasa.
    Devuelve el estado de la cuota para exponerlo en cabeceras.
    """
    limite = cuota_de(empresa_id)
    ahora = time.time()
    ventana = config.VENTANA_CUOTA_SEG
    clave = f"cuota:{empresa_id}"
    r = db.cache()

    # Ventana deslizante con un sorted set: se descartan los eventos
    # mas viejos que la ventana y se cuentan los que quedan.
    tuberia = r.pipeline()
    tuberia.zremrangebyscore(clave, 0, ahora - ventana)
    tuberia.zcard(clave)
    usados = tuberia.execute()[1]

    if usados + coste > limite:
        ttl = r.zrange(clave, 0, 0, withscores=True)
        espera = int(ventana - (ahora - ttl[0][1])) + 1 if ttl else ventana
        raise HTTPException(
            status_code=429,
            detail=(
                f"La empresa ha superado su cuota de {limite} consultas "
                f"por minuto. Reintenta en {espera} s."
            ),
            headers={"Retry-After": str(espera)},
        )

    tuberia = r.pipeline()
    for i in range(coste):
        tuberia.zadd(clave, {f"{ahora}:{i}": ahora})
    tuberia.expire(clave, ventana * 2)
    tuberia.execute()

    return {"limite": limite, "usadas": usados + coste, "ventana_seg": ventana}
