"""
Acceso a Postgres y Redis.

Lo importante de este modulo es 'conexion_empresa': abre una transaccion
y fija en ella las variables que leen las politicas RLS. A partir de ahi,
cualquier consulta que se haga dentro solo puede ver filas de esa empresa,
aunque el SQL se olvide de filtrar.
"""

from contextlib import contextmanager

import psycopg
import redis
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import config

_pool = ConnectionPool(
    config.DATABASE_URL,
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
)

_redis: redis.Redis | None = None


def abrir_recursos() -> None:
    global _redis
    _pool.open()
    _redis = redis.from_url(config.REDIS_URL, decode_responses=True)


def cerrar_recursos() -> None:
    _pool.close()
    if _redis is not None:
        _redis.close()


def cache() -> redis.Redis:
    if _redis is None:
        raise RuntimeError("Redis no inicializado")
    return _redis


@contextmanager
def conexion_empresa(empresa_id: str, rol: str = "usuario"):
    """
    Transaccion con el contexto de seguridad de la empresa fijado.

    set_config(..., true) las marca como locales a la transaccion, de modo
    que no se filtran a la siguiente peticion que reutilice la conexion
    del pool.
    """
    with _pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.empresa_id', %s, true), "
                    "       set_config('app.rol', %s, true)",
                    (empresa_id, rol),
                )
            yield conn


@contextmanager
def conexion_sin_contexto():
    """Para operaciones sobre tablas sin RLS (login, auditoria)."""
    with _pool.connection() as conn:
        with conn.transaction():
            yield conn


def comprobar_salud() -> dict:
    estado = {"postgres": False, "redis": False}
    try:
        with _pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            estado["postgres"] = True
    except psycopg.Error:
        pass
    try:
        estado["redis"] = bool(cache().ping())
    except Exception:
        pass
    return estado
