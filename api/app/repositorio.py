"""
Acceso a datos de la plataforma.

Dos reglas que se cumplen en TODAS las funciones de este modulo:

  E7. Ninguna funcion recibe 'empresa_id' como argumento libre: lo recibe
      de la sesion autenticada y lo usa para abrir la conexion con el
      contexto RLS. Aunque un SQL olvidara el WHERE, Postgres filtra.

  E6. Las metricas se leen del estado VIGENTE (correcciones aplicadas,
      cancelados excluidos) y siempre devuelven cuantos viajes de los
      agregados llevan correccion, para que el chatbot pueda avisar.
"""

from datetime import date

from app import db
from app.auth import Sesion

# Campos que un proveedor puede corregir (coincide con el CHECK de la tabla)
CAMPOS_CORREGIBLES = ("importe_total", "distancia", "pasajeros", "propina")


def _float(valor):
    return float(valor) if valor is not None else None


def _aviso(corregidos: int, cancelados: int = 0) -> str | None:
    """Texto de trazabilidad que exige E6."""
    if not corregidos and not cancelados:
        return None
    partes = []
    if corregidos:
        partes.append(f"{corregidos} viaje(s) con correccion aplicada")

    if cancelados:
        partes.append(f"{cancelados} viaje(s) cancelados excluidos")
    return "Esta cifra incluye " + " y ".join(partes) + "."


# ---------------------------------------------------------------------
# Consultas de metricas
# ---------------------------------------------------------------------

def resumen_metricas(
    sesion: Sesion, fecha: date | None = None, borough: str | None = None
) -> dict:
    """
    Resumen agregado. Lee de 'metricas_diarias', el cubo materializado que
    se recalcula de forma incremental cuando entra una correccion.
    """
    condiciones, parametros = [], []
    if fecha:
        condiciones.append("fecha = %s")
        parametros.append(fecha)
    if borough:
        condiciones.append("LOWER(borough) = LOWER(%s)")
        parametros.append(borough)
    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""

    sql = f"""
        SELECT COALESCE(SUM(num_viajes), 0)      AS num_viajes,
               COALESCE(SUM(num_cancelados), 0)  AS num_cancelados,
               COALESCE(SUM(num_corregidos), 0)  AS num_corregidos,
               COALESCE(SUM(importe_total), 0)   AS importe_total,
               CASE WHEN SUM(num_viajes) > 0
                    THEN SUM(importe_total) / SUM(num_viajes) END AS importe_medio,
               CASE WHEN SUM(num_viajes) > 0
                    THEN SUM(distancia_media * num_viajes) / SUM(num_viajes) END AS distancia_media,
               MAX(recalculada_en) AS actualizado_en
          FROM metricas_diarias
          {where}
    """
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(sql, parametros)
        fila = cur.fetchone()

    return {
        "empresa": sesion.empresa_id,
        "filtros": {"fecha": str(fecha) if fecha else "todas",
                    "borough": borough or "todos"},
        "num_viajes": fila["num_viajes"],
        "importe_total": round(_float(fila["importe_total"]) or 0, 2),
        "importe_medio": round(_float(fila["importe_medio"]) or 0, 2),
        "distancia_media": round(_float(fila["distancia_media"]) or 0, 2),
        "viajes_corregidos": fila["num_corregidos"],
        "viajes_cancelados": fila["num_cancelados"],
        "aviso_correcciones": _aviso(fila["num_corregidos"], fila["num_cancelados"]),
        "actualizado_en": (fila["actualizado_en"].isoformat()
                           if fila["actualizado_en"] else None),
    }


def metricas_por_zona(
    sesion: Sesion, limite: int = 8, fecha: date | None = None
) -> dict:
    """Ranking de zonas de recogida por numero de viajes."""
    condiciones = ["NOT vv.cancelado"]
    parametros: list = []
    if fecha:
        condiciones.append("vv.pickup_ts::date = %s")
        parametros.append(fecha)
    parametros.append(limite)

    sql = f"""
        SELECT z.zona, z.borough,
               COUNT(*)                                        AS num_viajes,
               AVG(vv.importe_total)                           AS importe_medio,
               COUNT(*) FILTER (WHERE vv.tiene_correccion)     AS corregidos
          FROM v_viajes_vigentes vv
          JOIN zonas z ON z.location_id = vv.pu_location_id
         WHERE {' AND '.join(condiciones)}
         GROUP BY z.zona, z.borough
         ORDER BY num_viajes DESC
         LIMIT %s
    """
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(sql, parametros)
        filas = cur.fetchall()

    corregidos = sum(f["corregidos"] for f in filas)
    return {
        "empresa": sesion.empresa_id,
        "zonas": [
            {
                "zona": f["zona"],
                "borough": f["borough"],
                "num_viajes": f["num_viajes"],
                "importe_medio": round(_float(f["importe_medio"]) or 0, 2),
                "corregidos": f["corregidos"],
            }
            for f in filas
        ],
        "aviso_correcciones": _aviso(corregidos),
    }


def metricas_por_borough(sesion: Sesion) -> dict:
    """Reparto por distrito. Alimenta la grafica del panel web."""
    sql = """
        SELECT borough,
               SUM(num_viajes)                          AS num_viajes,
               SUM(importe_total)                       AS importe_total,
               SUM(num_corregidos)                      AS corregidos,
               CASE WHEN SUM(num_viajes) > 0
                    THEN SUM(importe_total) / SUM(num_viajes) END AS importe_medio
          FROM metricas_diarias
         GROUP BY borough
         HAVING SUM(num_viajes) > 0
         ORDER BY num_viajes DESC
    """
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(sql)
        filas = cur.fetchall()

    return {
        "empresa": sesion.empresa_id,
        "boroughs": [
            {
                "borough": f["borough"],
                "num_viajes": f["num_viajes"],
                "importe_total": round(_float(f["importe_total"]) or 0, 2),
                "importe_medio": round(_float(f["importe_medio"]) or 0, 2),
                "corregidos": f["corregidos"],
            }
            for f in filas
        ],
        "aviso_correcciones": _aviso(sum(f["corregidos"] for f in filas)),
    }


def viajes_por_hora(sesion: Sesion) -> dict:
    """Distribucion horaria de la demanda."""
    sql = """
        SELECT EXTRACT(HOUR FROM pickup_ts)::int AS hora,
               COUNT(*)                          AS num_viajes,
               AVG(importe_total)                AS importe_medio
          FROM v_viajes_vigentes
         WHERE NOT cancelado
         GROUP BY hora
         ORDER BY hora
    """
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(sql)
        filas = cur.fetchall()

    return {
        "empresa": sesion.empresa_id,
        "horas": [
            {
                "hora": f["hora"],
                "num_viajes": f["num_viajes"],
                "importe_medio": round(_float(f["importe_medio"]) or 0, 2),
            }
            for f in filas
        ],
    }


def reparto_pagos(sesion: Sesion) -> dict:
    """Reparto por metodo de pago (codigos oficiales de la TLC)."""
    etiquetas = {
        1: "Tarjeta de credito", 2: "Efectivo", 3: "Sin cargo",
        4: "Disputa", 5: "Desconocido", 6: "Viaje anulado",
    }
    sql = """
        SELECT tipo_pago, COUNT(*) AS num_viajes, AVG(propina) AS propina_media
          FROM v_viajes_vigentes
         WHERE NOT cancelado
         GROUP BY tipo_pago
         ORDER BY num_viajes DESC
    """
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(sql)
        filas = cur.fetchall()

    return {
        "empresa": sesion.empresa_id,
        "pagos": [
            {
                "tipo_pago": f["tipo_pago"],
                "etiqueta": etiquetas.get(f["tipo_pago"], "Otro"),
                "num_viajes": f["num_viajes"],
                "propina_media": round(_float(f["propina_media"]) or 0, 2),
            }
            for f in filas
        ],
    }


def listar_viajes(sesion: Sesion, limite: int = 10) -> dict:
    """Ultimos viajes, para poder elegir uno que corregir."""
    sql = """
        SELECT vv.id, vv.pickup_ts, z.zona AS zona_origen,
               vv.distancia, vv.importe_total, vv.importe_total_original,
               vv.tiene_correccion, vv.cancelado
          FROM v_viajes_vigentes vv
          LEFT JOIN zonas z ON z.location_id = vv.pu_location_id
         ORDER BY vv.pickup_ts DESC
         LIMIT %s
    """
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(sql, (limite,))
        filas = cur.fetchall()

    return {
        "empresa": sesion.empresa_id,
        "viajes": [
            {
                "id": f["id"],
                "fecha": f["pickup_ts"].isoformat(),
                "zona_origen": f["zona_origen"],
                "distancia": _float(f["distancia"]),
                "importe_total": _float(f["importe_total"]),
                "importe_original": _float(f["importe_total_original"]),
                "corregido": f["tiene_correccion"],
                "cancelado": f["cancelado"],
            }
            for f in filas
        ],
    }


# ---------------------------------------------------------------------
# E6: registro y consulta de correcciones
# ---------------------------------------------------------------------

def registrar_correccion(
    sesion: Sesion, viaje_id: int, campo: str, valor_nuevo: float, motivo: str
) -> dict:
    """
    Anota una correccion. No toca la tabla 'viajes': el valor original se
    conserva intacto y la correccion queda como evento con su momento.
    El trigger de la base de datos recalcula SOLO el cubo afectado.
    """
    if campo not in CAMPOS_CORREGIBLES:
        return {"error": f"Campo no corregible. Validos: {', '.join(CAMPOS_CORREGIBLES)}"}

    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        # Se lee del estado VIGENTE, no de la tabla base: si el viaje ya
        # tenia una correccion previa, 'valor_original' debe ser el valor
        # que estaba en vigor en este momento. Asi el historial queda como
        # una cadena (v0 -> v1 -> v2) que explica cada salto, y el valor
        # tal y como se ingirio sigue intacto en la tabla 'viajes'.
        # El RLS ya impide ver viajes de otra empresa, asi que si no
        # aparece es que no existe o no es suyo: mismo mensaje en ambos
        # casos, para no revelar la existencia de datos ajenos.
        cur.execute(
            f"SELECT {campo} AS valor, cancelado FROM v_viajes_vigentes WHERE id = %s",
            (viaje_id,),
        )
        fila = cur.fetchone()
        if fila is None:
            return {"error": f"El viaje {viaje_id} no existe en tu empresa"}
        if fila["cancelado"]:
            return {"error": f"El viaje {viaje_id} esta cancelado y no admite correcciones"}

        valor_original = _float(fila["valor"])
        cur.execute(
            """INSERT INTO correcciones (viaje_id, empresa_id, tipo, campo,
                                         valor_original, valor_nuevo, motivo, aplicada_por)
               VALUES (%s, %s, 'correccion', %s, %s, %s, %s, %s)
               RETURNING id, aplicada_en""",
            (viaje_id, sesion.empresa_id, campo, valor_original,
             valor_nuevo, motivo, sesion.email),
        )
        correccion = cur.fetchone()

    return {
        "ok": True,
        "correccion_id": correccion["id"],
        "viaje_id": viaje_id,
        "campo": campo,
        "valor_original": valor_original,
        "valor_nuevo": valor_nuevo,
        "motivo": motivo,
        "aplicada_en": correccion["aplicada_en"].isoformat(),
        "nota": "Metricas del dia y distrito afectados recalculadas.",
    }


def cancelar_viaje(sesion: Sesion, viaje_id: int, motivo: str) -> dict:
    """Cancelacion: el viaje deja de contar en metricas pero no se borra."""
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT importe_total, cancelado FROM v_viajes_vigentes WHERE id = %s",
            (viaje_id,),
        )
        fila = cur.fetchone()
        if fila is None:
            return {"error": f"El viaje {viaje_id} no existe en tu empresa"}
        if fila["cancelado"]:
            return {"error": f"El viaje {viaje_id} ya estaba cancelado"}

        cur.execute(
            """INSERT INTO correcciones (viaje_id, empresa_id, tipo, campo,
                                         valor_original, valor_nuevo, motivo, aplicada_por)
               VALUES (%s, %s, 'cancelacion', NULL, %s, NULL, %s, %s)
               RETURNING id, aplicada_en""",
            (viaje_id, sesion.empresa_id, _float(fila["importe_total"]),
             motivo, sesion.email),
        )
        correccion = cur.fetchone()

    return {
        "ok": True,
        "correccion_id": correccion["id"],
        "viaje_id": viaje_id,
        "motivo": motivo,
        "aplicada_en": correccion["aplicada_en"].isoformat(),
        "nota": "El viaje se excluye de las metricas pero permanece en el historico.",
    }


def historial_correcciones(
    sesion: Sesion, viaje_id: int | None = None, limite: int = 20
) -> dict:
    """
    Responde a "por que ha cambiado esta cifra": devuelve valor original,
    valor corregido, motivo, autor y momento de cada cambio.
    """
    condiciones, parametros = [], []
    if viaje_id is not None:
        condiciones.append("c.viaje_id = %s")
        parametros.append(viaje_id)
    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""
    parametros.append(limite)

    sql = f"""
        SELECT c.id, c.viaje_id, c.tipo, c.campo, c.valor_original,
               c.valor_nuevo, c.motivo, c.aplicada_en, c.aplicada_por,
               v.pickup_ts, z.zona
          FROM correcciones c
          JOIN viajes v ON v.id = c.viaje_id
          LEFT JOIN zonas z ON z.location_id = v.pu_location_id
          {where}
         ORDER BY c.aplicada_en DESC
         LIMIT %s
    """
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(sql, parametros)
        filas = cur.fetchall()

    return {
        "empresa": sesion.empresa_id,
        "total": len(filas),
        "correcciones": [
            {
                "id": f["id"],
                "viaje_id": f["viaje_id"],
                "tipo": f["tipo"],
                "campo": f["campo"],
                "valor_original": _float(f["valor_original"]),
                "valor_nuevo": _float(f["valor_nuevo"]),
                "motivo": f["motivo"],
                "aplicada_en": f["aplicada_en"].isoformat(),
                "aplicada_por": f["aplicada_por"],
                "zona": f["zona"],
                "fecha_viaje": f["pickup_ts"].isoformat() if f["pickup_ts"] else None,
            }
            for f in filas
        ],
    }


# ---------------------------------------------------------------------
# E7: metricas globales, solo para rol auditor
# ---------------------------------------------------------------------

def metricas_globales(sesion: Sesion) -> dict:
    """Agregado entre empresas. La API ya ha comprobado el rol antes."""
    if not sesion.puede("auditor"):
        return {"error": "No autorizado: se requiere rol auditor"}

    sql = """
        SELECT empresa_id,
               SUM(num_viajes)     AS num_viajes,
               SUM(importe_total)  AS importe_total,
               SUM(num_corregidos) AS corregidos,
               SUM(num_cancelados) AS cancelados
          FROM metricas_diarias
         GROUP BY empresa_id
         ORDER BY num_viajes DESC
    """
    with db.conexion_empresa(sesion.empresa_id, sesion.rol) as conn, conn.cursor() as cur:
        cur.execute(sql)
        filas = cur.fetchall()

    return {
        "alcance": "todas las empresas",
        "empresas": [
            {
                "empresa": f["empresa_id"],
                "num_viajes": f["num_viajes"],
                "importe_total": round(_float(f["importe_total"]) or 0, 2),
                "corregidos": f["corregidos"],
                "cancelados": f["cancelados"],
            }
            for f in filas
        ],
        "total_viajes": sum(f["num_viajes"] for f in filas),
    }


# ---------------------------------------------------------------------
# Metricas de calidad de la plataforma (tarea "metricas propias")
# ---------------------------------------------------------------------

def metricas_calidad() -> dict:
    """
    Tres metricas de calidad elegidas por su relacion directa con las
    restricciones del equipo:

      1. Aislamiento (E7): % de intentos de acceso a datos ajenos que la
         plataforma bloquea. Debe ser 100%.
      2. Latencia de propagacion de correcciones (E6): tiempo entre que se
         registra una correccion y el cubo de metricas queda recalculado.
      3. Cobertura de trazabilidad (E6): % de correcciones que conservan
         valor original, motivo y autor.
    """
    # Estas metricas son de plataforma, no de una empresa: se leen con
    # contexto de auditor para que el RLS permita ver el conjunto.
    with db.conexion_empresa("plataforma", "auditor") as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) FILTER (WHERE NOT permitido) AS no_autorizados,
                      COUNT(*)                              AS total_accesos,
                      AVG(latencia_ms) FILTER (WHERE latencia_ms IS NOT NULL)
                          AS latencia_media
                 FROM auditoria_accesos"""
        )
        auditoria = cur.fetchone()

        cur.execute(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (
                          WHERE motivo IS NOT NULL
                            AND aplicada_por IS NOT NULL
                            AND valor_original IS NOT NULL) AS completas
                 FROM correcciones"""
        )
        correcciones = cur.fetchone()

        # La propagacion se mide por cubo, comparando su ULTIMA correccion
        # con su ultimo recalculo. Comparar cada correccion con el recalculo
        # mas reciente del cubo inflaria la cifra, porque una correccion
        # posterior vuelve a tocar el mismo cubo.
        cur.execute(
            """WITH ultima_por_cubo AS (
                   SELECT c.empresa_id,
                          v.pickup_ts::date AS fecha,
                          z.borough,
                          MAX(c.aplicada_en) AS ultima_correccion
                     FROM correcciones c
                     JOIN viajes v ON v.id = c.viaje_id
                     JOIN zonas z  ON z.location_id = v.pu_location_id
                    GROUP BY 1, 2, 3
               )
               SELECT COUNT(*) AS cubos,
                      AVG(EXTRACT(EPOCH FROM (m.recalculada_en - u.ultima_correccion)) * 1000)
                          AS propagacion_ms
                 FROM ultima_por_cubo u
                 JOIN metricas_diarias m
                   ON m.empresa_id = u.empresa_id
                  AND m.fecha = u.fecha
                  AND m.borough = u.borough"""
        )
        propagacion = cur.fetchone()

    total_corr = correcciones["total"] or 0
    no_autorizados = auditoria["no_autorizados"] or 0
    total_accesos = auditoria["total_accesos"] or 0
    return {
        "aislamiento_entre_empresas": {
            "descripcion": (
                "Peticiones no autorizadas (credenciales, rol o empresa ajena) "
                "rechazadas antes de devolver dato alguno"
            ),
            "peticiones_rechazadas": no_autorizados,
            "accesos_totales": total_accesos,
            "porcentaje_rechazado": (f"{100 * no_autorizados / total_accesos:.1f}%"
                                     if total_accesos else "sin accesos aun"),
            "latencia_media_consulta_ms": round(_float(auditoria["latencia_media"]) or 0, 1),
            "nota": (
                "El objetivo (cero fugas entre empresas) se verifica ademas en la "
                "bateria de pruebas, que comprueba que los conjuntos de viajes de "
                "las dos empresas son disjuntos y suman el total ingerido."
            ),
        },
        "propagacion_correcciones": {
            "descripcion": (
                "Retardo entre registrar una correccion y tener el cubo de "
                "metricas ya recalculado"
            ),
            "latencia_media_ms": round(_float(propagacion["propagacion_ms"]) or 0, 1),
            "cubos_medidos": propagacion["cubos"] or 0,
            "correcciones_registradas": total_corr,
        },
        "trazabilidad_correcciones": {
            "descripcion": "Correcciones con valor original, motivo y autor conservados",
            "completas": correcciones["completas"] or 0,
            "total": total_corr,
            "cobertura": (f"{100 * correcciones['completas'] / total_corr:.0f}%"
                          if total_corr else "sin correcciones aun"),
        },
    }


_catalogo_empresas: dict[str, str] = {}


def catalogo_empresas() -> dict[str, str]:
    """id -> nombre de todas las empresas. La tabla no lleva RLS: son
    metadatos del catalogo, no datos de negocio."""
    global _catalogo_empresas
    if not _catalogo_empresas:
        with db.conexion_sin_contexto() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, nombre FROM empresas")
            _catalogo_empresas = {f["id"]: f["nombre"] for f in cur.fetchall()}
    return _catalogo_empresas


def registrar_auditoria(
    usuario: str | None, empresa_id: str | None, accion: str,
    detalle: str, permitido: bool, latencia_ms: int | None = None,
) -> None:
    try:
        with db.conexion_sin_contexto() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO auditoria_accesos
                       (usuario, empresa_id, accion, detalle, permitido, latencia_ms)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (usuario, empresa_id, accion, detalle[:500], permitido, latencia_ms),
            )
    except Exception:
        # La auditoria nunca debe tumbar una peticion de negocio.
        pass
