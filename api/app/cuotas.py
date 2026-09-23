"""
Limitador de consumo por empresa (E7).

Requisito: "Evitar que una empresa consuma todos los recursos disponibles
para las demas". Implementado como ventana deslizante en Redis, con la
cuota definida por empresa en la tabla 'empresas'.

La cuota se aplica POR EMPRESA, no por usuario: si una empresa tiene diez
usuarios lanzando consultas, entre todos no pueden agotar la plataforma.

La comprobacion y el consumo se hacen en un unico script Lua, que Redis
ejecuta de forma atomica. La version anterior leia el contador y luego
escribia en dos viajes separados: con varias peticiones simultaneas (o
varias replicas de la API) todas podian leer "queda cuota" a la vez y
pasarse del limite.
"""

import time
import uuid

from fastapi import HTTPException

from app import config, db

_cuotas_cache: dict[str, int] = {}

_SCRIPT = """
local clave   = KEYS[1]
local ahora   = tonumber(ARGV[1])
local ventana = tonumber(ARGV[2])
local limite  = tonumber(ARGV[3])
local coste   = tonumber(ARGV[4])
local id      = ARGV[5]

redis.call('ZREMRANGEBYSCORE', clave, '-inf', ahora - ventana)
local usados = redis.call('ZCARD', clave)
if usados + coste > limite then
    local primero = redis.call('ZRANGE', clave, 0, 0, 'WITHSCORES')
    return {0, usados, primero[2] or tostring(ahora)}
end
for i = 1, coste do
    redis.call('ZADD', clave, ahora, id .. ':' .. i)
end
redis.call('EXPIRE', clave, math.ceil(ventana * 2))
return {1, usados + coste, tostring(ahora)}
"""

_script = None


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
    global _script
    if _script is None or _script.registered_client is not db.cache():
        _script = db.cache().register_script(_SCRIPT)

    limite = cuota_de(empresa_id)
    ventana = config.VENTANA_CUOTA_SEG
    ahora = time.time()
    permitido, usadas, primero = _script(
        keys=[f"cuota:{empresa_id}"],
        args=[ahora, ventana, limite, coste, uuid.uuid4().hex],
    )
    usadas = int(usadas)

    if not int(permitido):
        espera = max(1, int(ventana - (ahora - float(primero))) + 1)
        raise HTTPException(
            status_code=429,
            detail=(
                f"Tu empresa ha superado su cuota de {limite} consultas "
                f"por minuto. Reintenta en {espera} s."
            ),
            headers={"Retry-After": str(espera)},
        )

    return {
        "limite": limite,
        "usadas": usadas,
        "restantes": max(0, limite - usadas),
        "ventana_seg": ventana,
    }
