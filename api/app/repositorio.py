"""
Acceso a datos de la plataforma.

Dos reglas que se cumplen en TODAS las funciones de este modulo:

  E7. Ninguna funcion recibe 'empresa_id' como argumento libre: lo recibe
      de la sesion autenticada y lo usa para abrir la conexion con el
      contexto RLS. Aunque un SQL olvidara el WHERE, Postgres filtra.

  E6. Las metricas se leen del estado VIGENTE (correcciones aplicadas,
      cancelados excluidos) y siempre devuelven cuantos viajes de los
      agregados llevan correccion, para que el chatbot pueda avisar.

Las consultas estan separadas en funciones internas '_q_*' que reciben un
cursor, de modo que el panel web puede pedir todo en UNA transaccion (y
una sola unidad de cuota) en lugar de cinco peticiones.

Los importes son dolares (USD): el dataset es de taxis de Nueva York.
"""

import logging
from datetime import UTC, date

import psycopg

from app import db
from app.auth import Sesion

# Campos que un proveedor puede corregir (coincide con el CHECK de la tabla)
CAMPOS_CORREGIBLES = ("importe_total", "distancia", "pasajeros", "propina")

NOMBRE_CAMPO = {
    "importe_total": "importe total",
    "distancia": "distancia",
    "pasajeros": "pasajeros",
    "propina": "propina",
}

# Rangos admisibles de una correccion. Un valor fuera de rango casi siempre
# es un error de tecleo (15000 en vez de 150,00) y, una vez registrado, el
# historial es de solo-anadir: mejor pararlo antes.
RANGOS = {
    "importe_total": (-1000.0, 10000.0),   # hay reembolsos negativos en los datos
    "distancia": (0.0, 500.0),
    "pasajeros": (0.0, 9.0),
    "propina": (0.0, 1000.0),
}

ETIQUETAS_PAGO = {
    1: "Tarjeta de crédito", 2: "Efectivo", 3: "Sin cargo",
    4: "Disputa", 5: "Desconocido", 6: "Viaje anulado",
}


def _float(valor):
    return float(valor) if valor is not None else None


def _r2(valor) -> float:
    return round(_float(valor) or 0, 2)


def _error(mensaje: str, codigo: int) -> dict:
    """Error de negocio. 'codigo' es el estado HTTP que le corresponde."""
    return {"error": mensaje, "codigo": codigo}


def _aviso(corregidos: int, cancelados: int = 0) -> str | None:
    """Texto de trazabilidad que exige E6."""
    if not corregidos and not cancelados:
        return None
    partes = []
    if corregidos:
        partes.append(f"{corregidos} viaje(s) con corrección aplicada")
    if cancelados:
        partes.append(f"{cancelados} viaje(s) cancelado(s) excluido(s)")
    return "Esta cifra incluye " + " y ".join(partes) + "."


def _conexion(sesion: Sesion):
    return db.conexion_empresa(sesion.empresa_id, sesion.rol)


# ---------------------------------------------------------------------
# Consultas internas (reciben cursor)
# ---------------------------------------------------------------------

def _q_resumen(cur, sesion: Sesion, fecha: date | None, borough: str | None) -> dict:
    condiciones, parametros = [], []
    if fecha:
        condiciones.append("fecha = %s")
        parametros.append(fecha)
    if borough:
        condiciones.append("LOWER(borough) = LOWER(%s)")
        parametros.append(borough)
    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""

    cur.execute(
        f"""
        SELECT COALESCE(SUM(num_viajes), 0)      AS num_viajes,
               COALESCE(SUM(num_cancelados), 0)  AS num_cancelados,
               COALESCE(SUM(num_corregidos), 0)  AS num_corregidos,
               COALESCE(SUM(importe_total), 0)   AS importe_total,
               CASE WHEN SUM(num_viajes) > 0
                    THEN SUM(importe_total) / SUM(num_viajes) END AS importe_medio,
               CASE WHEN SUM(num_viajes) > 0
                    THEN SUM(distancia_media * num_viajes) / SUM(num_viajes) END AS distancia_media,
               CASE WHEN SUM(num_viajes) > 0
                    THEN SUM(propina_media * num_viajes) / SUM(num_viajes) END AS propina_media,
               MAX(recalculada_en) AS actualizado_en
          FROM metricas_diarias
          {where}
        """,
        parametros,
    )
    fila = cur.fetchone()
    return {
        "empresa": sesion.empresa_id,
        "alcance": "todas las empresas" if sesion.puede("ver_global") else "tu empresa",
        "filtros": {"fecha": str(fecha) if fecha else "todas",
                    "borough": borough or "todos"},
        "num_viajes": fila["num_viajes"],
        "importe_total": _r2(fila["importe_total"]),
        "importe_medio": _r2(fila["importe_medio"]),
        "distancia_media": _r2(fila["distancia_media"]),
        "propina_media": _r2(fila["propina_media"]),
        "moneda": "USD",
        "viajes_corregidos": fila["num_corregidos"],
        "viajes_cancelados": fila["num_cancelados"],
        "aviso_correcciones": _aviso(fila["num_corregidos"], fila["num_cancelados"]),
        "actualizado_en": (fila["actualizado_en"].isoformat()
                           if fila["actualizado_en"] else None),
    }


def _q_zonas(cur, sesion: Sesion, limite: int, fecha: date | None,
             borough: str | None) -> dict:
    condiciones = ["NOT vv.cancelado"]
    parametros: list = []
    if fecha:
        condiciones.append("vv.pickup_ts::date = %s")
        parametros.append(fecha)
    if borough:
        condiciones.append("LOWER(z.borough) = LOWER(%s)")
        parametros.append(borough)
    parametros.append(limite)
    cur.execute(
        f"""
        SELECT z.zona, z.borough,
               COUNT(*)                                        AS num_viajes,
               AVG(vv.importe_total)                           AS importe_medio,
               COUNT(*) FILTER (WHERE vv.tiene_correccion)     AS corregidos
          FROM v_viajes_vigentes vv
          JOIN zonas z ON z.location_id = vv.pu_location_id
         WHERE {' AND '.join(condiciones)}
         GROUP BY z.zona, z.borough
         ORDER BY num_viajes DESC, z.zona
         LIMIT %s
        """,
        parametros,
    )
    filas = cur.fetchall()
    return {
        "empresa": sesion.empresa_id,
        "filtros": {"fecha": str(fecha) if fecha else "todas",
                    "borough": borough or "todos"},
        "zonas": [
            {
                "zona": f["zona"],
                "borough": f["borough"],
                "num_viajes": f["num_viajes"],
                "importe_medio": _r2(f["importe_medio"]),
                "corregidos": f["corregidos"],
            }
            for f in filas
        ],
        "aviso_correcciones": _aviso(sum(f["corregidos"] for f in filas)),
    }


def _q_boroughs(cur, sesion: Sesion) -> dict:
    cur.execute(
        """
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
    )
    filas = cur.fetchall()
    return {
        "empresa": sesion.empresa_id,
        "boroughs": [
            {
                "borough": f["borough"],
                "num_viajes": f["num_viajes"],
                "importe_total": _r2(f["importe_total"]),
                "importe_medio": _r2(f["importe_medio"]),
                "corregidos": f["corregidos"],
            }
            for f in filas
        ],
        "aviso_correcciones": _aviso(sum(f["corregidos"] for f in filas)),
    }


def _q_horas(cur, sesion: Sesion) -> dict:
    cur.execute(
        """
        SELECT EXTRACT(HOUR FROM pickup_ts)::int AS hora,
               COUNT(*)                          AS num_viajes,
               AVG(importe_total)                AS importe_medio
          FROM v_viajes_vigentes
         WHERE NOT cancelado
         GROUP BY hora
         ORDER BY hora
        """
    )
    filas = cur.fetchall()
    return {
        "empresa": sesion.empresa_id,
        "horas": [
            {"hora": f["hora"], "num_viajes": f["num_viajes"],
             "importe_medio": _r2(f["importe_medio"])}
            for f in filas
        ],
    }


def _q_pagos(cur, sesion: Sesion) -> dict:
    cur.execute(
        """
        SELECT tipo_pago, COUNT(*) AS num_viajes, AVG(propina) AS propina_media
          FROM v_viajes_vigentes
         WHERE NOT cancelado
         GROUP BY tipo_pago
         ORDER BY num_viajes DESC
        """
    )
    filas = cur.fetchall()
    return {
        "empresa": sesion.empresa_id,
        "pagos": [
            {
                "tipo_pago": f["tipo_pago"],
                "etiqueta": ETIQUETAS_PAGO.get(f["tipo_pago"], "Otro"),
                "num_viajes": f["num_viajes"],
                "propina_media": _r2(f["propina_media"]),
            }
            for f in filas
        ],
    }


def _q_viajes(cur, sesion: Sesion, limite: int) -> dict:
    cur.execute(
        """
        SELECT vv.id, vv.empresa_id, vv.pickup_ts, z.zona AS zona_origen,
               vv.distancia, vv.pasajeros, vv.propina,
               vv.importe_total, vv.importe_total_original,
               vv.tiene_correccion, vv.cancelado
          FROM v_viajes_vigentes vv
          LEFT JOIN zonas z ON z.location_id = vv.pu_location_id
         ORDER BY vv.pickup_ts DESC, vv.id DESC
         LIMIT %s
        """,
        (limite,),
    )
    filas = cur.fetchall()
    return {
        "empresa": sesion.empresa_id,
        "viajes": [
            {
                "id": f["id"],
                "fecha": f["pickup_ts"].isoformat(),
                "zona_origen": f["zona_origen"],
                "distancia": _float(f["distancia"]),
                "pasajeros": _float(f["pasajeros"]),
                "propina": _float(f["propina"]),
                "importe_total": _float(f["importe_total"]),
                "importe_original": _float(f["importe_total_original"]),
                "corregido": f["tiene_correccion"],
                "cancelado": f["cancelado"],
            }
            for f in filas
        ],
    }


def _q_historial(cur, sesion: Sesion, viaje_id: int | None, limite: int) -> dict:
    condiciones, parametros = [], []
    if viaje_id is not None:
        condiciones.append("c.viaje_id = %s")
        parametros.append(viaje_id)
    where = ("WHERE " + " AND ".join(condiciones)) if condiciones else ""
    parametros.append(limite)
    cur.execute(
        f"""
        SELECT c.id, c.viaje_id, c.tipo, c.campo, c.valor_original,
               c.valor_nuevo, c.motivo, c.aplicada_en, c.aplicada_por,
               v.pickup_ts, z.zona,
               COUNT(*) OVER () AS total
          FROM correcciones c
          JOIN viajes v ON v.id = c.viaje_id
          LEFT JOIN zonas z ON z.location_id = v.pu_location_id
          {where}
         ORDER BY c.aplicada_en DESC, c.id DESC
         LIMIT %s
        """,
        parametros,
    )
    filas = cur.fetchall()
    return {
        "empresa": sesion.empresa_id,
        "total": filas[0]["total"] if filas else 0,
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


def _q_globales(cur) -> dict:
    cur.execute(
        """
        SELECT m.empresa_id, e.nombre,
               SUM(m.num_viajes)     AS num_viajes,
               SUM(m.importe_total)  AS importe_total,
               SUM(m.num_corregidos) AS corregidos,
               SUM(m.num_cancelados) AS cancelados
          FROM metricas_diarias m
          JOIN empresas e ON e.id = m.empresa_id
         GROUP BY m.empresa_id, e.nombre
         ORDER BY num_viajes DESC
        """
    )
    filas = cur.fetchall()
    return {
        "alcance": "todas las empresas",
        "empresas": [
            {
                "empresa": f["empresa_id"],
                "nombre": f["nombre"],
                "num_viajes": f["num_viajes"],
                "importe_total": _r2(f["importe_total"]),
                "importe_medio": (_r2(f["importe_total"] / f["num_viajes"])
                                  if f["num_viajes"] else 0),
                "corregidos": f["corregidos"],
                "cancelados": f["cancelados"],
            }
            for f in filas
        ],
        "total_viajes": sum(f["num_viajes"] for f in filas),
        "moneda": "USD",
    }


# ---------------------------------------------------------------------
# Consultas publicas
# ---------------------------------------------------------------------

def resumen_metricas(
    sesion: Sesion, fecha: date | None = None, borough: str | None = None
) -> dict:
    """
    Resumen agregado. Lee de 'metricas_diarias', el cubo materializado que
    se recalcula de forma incremental cuando entra una correccion.
    """
    with _conexion(sesion) as conn, conn.cursor() as cur:
        return _q_resumen(cur, sesion, fecha, borough)


def metricas_por_zona(
    sesion: Sesion, limite: int = 8, fecha: date | None = None,
    borough: str | None = None,
) -> dict:
    """Ranking de zonas de recogida por numero de viajes."""
    limite = max(1, min(int(limite), 25))
    with _conexion(sesion) as conn, conn.cursor() as cur:
        return _q_zonas(cur, sesion, limite, fecha, borough)


def metricas_por_borough(sesion: Sesion) -> dict:
    """Reparto por distrito. Alimenta la grafica del panel web."""
    with _conexion(sesion) as conn, conn.cursor() as cur:
        return _q_boroughs(cur, sesion)


def viajes_por_hora(sesion: Sesion) -> dict:
    """Distribucion horaria de la demanda."""
    with _conexion(sesion) as conn, conn.cursor() as cur:
        return _q_horas(cur, sesion)


def reparto_pagos(sesion: Sesion) -> dict:
    """Reparto por metodo de pago (codigos oficiales de la TLC)."""
    with _conexion(sesion) as conn, conn.cursor() as cur:
        return _q_pagos(cur, sesion)


def listar_viajes(sesion: Sesion, limite: int = 10) -> dict:
    """Ultimos viajes, para poder elegir uno que corregir."""
    limite = max(1, min(int(limite), 50))
    with _conexion(sesion) as conn, conn.cursor() as cur:
        return _q_viajes(cur, sesion, limite)


def historial_correcciones(
    sesion: Sesion, viaje_id: int | None = None, limite: int = 20
) -> dict:
    """
    Responde a "por que ha cambiado esta cifra": devuelve valor original,
    valor corregido, motivo, autor y momento de cada cambio.
    """
    limite = max(1, min(int(limite), 100))
    with _conexion(sesion) as conn, conn.cursor() as cur:
        return _q_historial(cur, sesion, viaje_id, limite)


def detalle_viaje(sesion: Sesion, viaje_id: int) -> dict:
    """
    Ficha de un viaje: valores tal y como se ingirieron, valores vigentes
    y la cadena completa de cambios. Es la respuesta mas directa a
    "por que ha cambiado este viaje".
    """
    with _conexion(sesion) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT vv.*, zo.zona AS zona_origen, zo.borough AS borough_origen,
                   zd.zona AS zona_destino
              FROM v_viajes_vigentes vv
              LEFT JOIN zonas zo ON zo.location_id = vv.pu_location_id
              LEFT JOIN zonas zd ON zd.location_id = vv.do_location_id
             WHERE vv.id = %s
            """,
            (viaje_id,),
        )
        f = cur.fetchone()
        if f is None:
            return _error(f"El viaje {viaje_id} no existe en tu empresa", 404)
        historial = _q_historial(cur, sesion, viaje_id, 50)

    return {
        "empresa": sesion.empresa_id,
        "viaje_id": f["id"],
        "fecha": f["pickup_ts"].isoformat(),
        "zona_origen": f["zona_origen"],
        "borough_origen": f["borough_origen"],
        "zona_destino": f["zona_destino"],
        "tipo_pago": ETIQUETAS_PAGO.get(f["tipo_pago"], "Otro"),
        "campos": {
            campo: {
                "vigente": _float(f[campo]),
                "original": _float(f[f"{campo}_original"]),
                "corregido": _float(f[campo]) != _float(f[f"{campo}_original"]),
            }
            for campo in CAMPOS_CORREGIBLES
        },
        "cancelado": f["cancelado"],
        "num_correcciones": f["num_correcciones"],
        "historial": historial["correcciones"],
        "moneda": "USD",
    }


def fechas_disponibles(sesion: Sesion) -> list[date]:
    """Fechas con viajes (para resolver '1 de enero' sin ano)."""
    with _conexion(sesion) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT fecha FROM metricas_diarias ORDER BY fecha")
        return [f["fecha"] for f in cur.fetchall()]


def panel(sesion: Sesion) -> dict:
    """
    Todo lo que necesita el panel web en UNA transaccion. Antes el panel
    hacia cinco peticiones por refresco, cada una contaba contra la cuota
    de la empresa, y un operador que aplicaba tres correcciones seguidas
    se quedaba bloqueado con 429.
    """
    with _conexion(sesion) as conn, conn.cursor() as cur:
        datos = {
            "empresa": sesion.empresa_id,
            "resumen": _q_resumen(cur, sesion, None, None),
            "boroughs": _q_boroughs(cur, sesion)["boroughs"],
            "pagos": _q_pagos(cur, sesion)["pagos"],
            "zonas": _q_zonas(cur, sesion, 6, None, None)["zonas"],
            "correcciones": _q_historial(cur, sesion, None, 15),
        }
    datos["cadena"] = {k: v for k, v in verificar_cadena(sesion).items() if k != "algoritmo"}
    with _conexion(sesion) as conn, conn.cursor() as cur:
        if sesion.puede("corregir"):
            datos["viajes"] = _q_viajes(cur, sesion, 40)["viajes"]
        if sesion.puede("ver_global"):
            datos["globales"] = _q_globales(cur)
    return datos


# ---------------------------------------------------------------------
# E6: registro de correcciones
# ---------------------------------------------------------------------

def validar_valor(campo: str, valor: float) -> str | None:
    """Devuelve el motivo del rechazo, o None si el valor es admisible."""
    if campo not in CAMPOS_CORREGIBLES:
        return f"Campo no corregible. Válidos: {', '.join(CAMPOS_CORREGIBLES)}"
    minimo, maximo = RANGOS[campo]
    if not (minimo <= valor <= maximo):
        return (f"El valor {valor:g} no es razonable para {NOMBRE_CAMPO[campo]} "
                f"(admitido entre {minimo:g} y {maximo:g})")
    if campo == "pasajeros" and valor != int(valor):
        return "El número de pasajeros debe ser un entero"
    return None


def previsualizar_correccion(sesion: Sesion, viaje_id: int, campo: str,
                             valor_nuevo: float) -> dict:
    """
    Comprueba una correccion SIN registrarla y devuelve el valor vigente.
    La usa el chatbot para pedir confirmacion antes de escribir.
    """
    problema = validar_valor(campo, valor_nuevo)
    if problema:
        return _error(problema, 422)
    with _conexion(sesion) as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {campo} AS valor, cancelado FROM v_viajes_vigentes WHERE id = %s",
            (viaje_id,),
        )
        fila = cur.fetchone()
    if fila is None:
        return _error(f"El viaje {viaje_id} no existe en tu empresa", 404)
    if fila["cancelado"]:
        return _error(f"El viaje {viaje_id} está cancelado y no admite correcciones", 409)
    actual = _float(fila["valor"])
    if actual is not None and abs(actual - valor_nuevo) < 0.005:
        return _error(f"El viaje {viaje_id} ya tiene {NOMBRE_CAMPO[campo]} = {valor_nuevo:g}", 409)
    return {"ok": True, "viaje_id": viaje_id, "campo": campo,
            "valor_actual": actual, "valor_nuevo": valor_nuevo}


def registrar_correccion(
    sesion: Sesion, viaje_id: int, campo: str, valor_nuevo: float, motivo: str
) -> dict:
    """
    Anota una correccion. No toca la tabla 'viajes': el valor original se
    conserva intacto y la correccion queda como evento con su momento.
    El trigger de la base de datos recalcula SOLO el cubo afectado.
    """
    if not sesion.puede("corregir"):
        return _error("Tu rol no permite registrar correcciones", 403)
    motivo = (motivo or "").strip()
    if len(motivo) < 3:
        return _error("Indica el motivo de la corrección (mínimo 3 caracteres)", 422)
    problema = validar_valor(campo, valor_nuevo)
    if problema:
        return _error(problema, 422)

    with _conexion(sesion) as conn, conn.cursor() as cur:
        # Se lee del estado VIGENTE, no de la tabla base: si el viaje ya
        # tenia una correccion previa, 'valor_original' debe ser el valor
        # que estaba en vigor en este momento. Asi el historial queda como
        # una cadena (v0 -> v1 -> v2) que explica cada salto, y el valor
        # tal y como se ingirio sigue intacto en la tabla 'viajes'.
        # El RLS ya impide ver viajes de otra empresa, asi que si no
        # aparece es que no existe o no es suyo: mismo mensaje en ambos
        # casos, para no revelar la existencia de datos ajenos.
        # FOR UPDATE no es posible sobre la vista; el bloqueo consultivo
        # por viaje serializa dos correcciones simultaneas del mismo viaje
        # para que la cadena v0 -> v1 -> v2 no se rompa.
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (viaje_id,))
        cur.execute(
            f"SELECT {campo} AS valor, cancelado FROM v_viajes_vigentes WHERE id = %s",
            (viaje_id,),
        )
        fila = cur.fetchone()
        if fila is None:
            return _error(f"El viaje {viaje_id} no existe en tu empresa", 404)
        if fila["cancelado"]:
            return _error(f"El viaje {viaje_id} está cancelado y no admite correcciones", 409)

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
        resumen = _q_resumen(cur, sesion, None, None)

    return {
        "ok": True,
        "correccion_id": correccion["id"],
        "viaje_id": viaje_id,
        "campo": campo,
        "valor_original": valor_original,
        "valor_nuevo": valor_nuevo,
        "motivo": motivo,
        "aplicada_en": correccion["aplicada_en"].isoformat(),
        "nota": "Métricas del día y distrito afectados recalculadas.",
        "resumen_actualizado": resumen,
    }


def cancelar_viaje(sesion: Sesion, viaje_id: int, motivo: str) -> dict:
    """Cancelacion: el viaje deja de contar en metricas pero no se borra."""
    if not sesion.puede("corregir"):
        return _error("Tu rol no permite cancelar viajes", 403)
    motivo = (motivo or "").strip()
    if len(motivo) < 3:
        return _error("Indica el motivo de la cancelación (mínimo 3 caracteres)", 422)

    try:
        with _conexion(sesion) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT importe_total, cancelado FROM v_viajes_vigentes WHERE id = %s",
                (viaje_id,),
            )
            fila = cur.fetchone()
            if fila is None:
                return _error(f"El viaje {viaje_id} no existe en tu empresa", 404)
            if fila["cancelado"]:
                return _error(f"El viaje {viaje_id} ya estaba cancelado", 409)

            cur.execute(
                """INSERT INTO correcciones (viaje_id, empresa_id, tipo, campo,
                                             valor_original, valor_nuevo, motivo, aplicada_por)
                   VALUES (%s, %s, 'cancelacion', NULL, %s, NULL, %s, %s)
                   RETURNING id, aplicada_en""",
                (viaje_id, sesion.empresa_id, _float(fila["importe_total"]),
                 motivo, sesion.email),
            )
            correccion = cur.fetchone()
            resumen = _q_resumen(cur, sesion, None, None)
    except psycopg.errors.UniqueViolation:
        # Dos cancelaciones simultaneas: el indice unico para la segunda.
        return _error(f"El viaje {viaje_id} ya estaba cancelado", 409)

    return {
        "ok": True,
        "correccion_id": correccion["id"],
        "viaje_id": viaje_id,
        "motivo": motivo,
        "importe_excluido": _float(fila["importe_total"]),
        "aplicada_en": correccion["aplicada_en"].isoformat(),
        "nota": "El viaje se excluye de las métricas pero permanece en el histórico.",
        "resumen_actualizado": resumen,
    }


# ---------------------------------------------------------------------
# E7: metricas globales, solo para rol auditor
# ---------------------------------------------------------------------

def metricas_globales(sesion: Sesion) -> dict:
    """Agregado entre empresas. La API ya ha comprobado el rol antes."""
    if not sesion.puede("ver_global"):
        return _error("No autorizado: se requiere rol auditor", 403)
    with _conexion(sesion) as conn, conn.cursor() as cur:
        return _q_globales(cur)


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

    Mas un informe de la calidad de los datos de origen (ingesta).
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

        cur.execute("SELECT COUNT(*) AS cubos FROM metricas_diarias")
        total_cubos = cur.fetchone()["cubos"]

        cur.execute(
            """SELECT fichero, sha256, filas_leidas, filas_cargadas,
                      filas_descartadas, anomalias, ejecutada_en
                 FROM ingestas ORDER BY ejecutada_en DESC LIMIT 1"""
        )
        ingesta = cur.fetchone()

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
                                     if total_accesos else "sin accesos aún"),
            "latencia_media_consulta_ms": round(_float(auditoria["latencia_media"]) or 0, 1),
            "nota": (
                "El objetivo (cero fugas entre empresas) se verifica además en la "
                "batería de pruebas, que comprueba que los conjuntos de viajes de "
                "las dos empresas son disjuntos y suman el total ingerido."
            ),
        },
        "propagacion_correcciones": {
            "descripcion": (
                "Retardo entre registrar una corrección y tener el cubo de "
                "métricas ya recalculado"
            ),
            "latencia_media_ms": round(_float(propagacion["propagacion_ms"]) or 0, 2),
            "cubos_recalculados": propagacion["cubos"] or 0,
            "cubos_totales": total_cubos,
            "correcciones_registradas": total_corr,
        },
        "trazabilidad_correcciones": {
            "descripcion": "Correcciones con valor original, motivo y autor conservados",
            "completas": correcciones["completas"] or 0,
            "total": total_corr,
            "cobertura": (f"{100 * correcciones['completas'] / total_corr:.0f}%"
                          if total_corr else "sin correcciones aún"),
        },
        "consistencia_cubo": consistencia_cubo(),
        "calidad_ingesta": (
            {
                "fichero": ingesta["fichero"],
                "sha256": ingesta["sha256"],
                "filas_leidas": ingesta["filas_leidas"],
                "filas_cargadas": ingesta["filas_cargadas"],
                "filas_descartadas": ingesta["filas_descartadas"],
                "anomalias_detectadas": ingesta["anomalias"],
                "ejecutada_en": ingesta["ejecutada_en"].isoformat(),
                "nota": ("Las anomalías se cargan igualmente: son hechos tal y "
                         "como los envió el proveedor, y E6 permite corregirlos."),
            }
            if ingesta else None
        ),
    }


_catalogo_empresas: list[dict] = []


def catalogo_empresas() -> list[dict]:
    """Empresas de la plataforma (id, nombre, si tiene datos propios).
    La tabla no lleva RLS: son metadatos del catalogo, no datos de negocio."""
    global _catalogo_empresas
    if not _catalogo_empresas:
        with db.conexion_sin_contexto() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, nombre, vendor_id_origen FROM empresas ORDER BY id")
            _catalogo_empresas = [
                {"id": f["id"], "nombre": f["nombre"],
                 "tiene_datos": f["vendor_id_origen"] is not None}
                for f in cur.fetchall()
            ]
    return _catalogo_empresas


def nombre_empresa(empresa_id: str) -> str:
    for e in catalogo_empresas():
        if e["id"] == empresa_id:
            return e["nombre"]
    return empresa_id


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
                (usuario, empresa_id, accion, (detalle or "")[:500], permitido, latencia_ms),
            )
    except Exception as exc:
        # La auditoria nunca debe tumbar una peticion de negocio, pero se deja rastro.
        logging.getLogger("pids").error("no se pudo auditar %s: %s", accion, exc)


# ---------------------------------------------------------------------
# E6: viaje en el tiempo
# ---------------------------------------------------------------------

_SQL_RESUMEN_EN = """
    SELECT COUNT(*) FILTER (WHERE NOT ve.cancelado)                        AS num_viajes,
           COUNT(*) FILTER (WHERE ve.cancelado)                            AS num_cancelados,
           COUNT(*) FILTER (WHERE ve.tiene_correccion AND NOT ve.cancelado) AS num_corregidos,
           COALESCE(SUM(ve.importe_total) FILTER (WHERE NOT ve.cancelado), 0) AS importe_total,
           AVG(ve.importe_total) FILTER (WHERE NOT ve.cancelado)            AS importe_medio
      FROM viajes_en(%s) ve
      JOIN zonas z ON z.location_id = ve.pu_location_id
"""


def _fila_resumen(f) -> dict:
    return {
        "num_viajes": f["num_viajes"],
        "importe_total": _r2(f["importe_total"]),
        "importe_medio": _r2(f["importe_medio"]),
        "viajes_corregidos": f["num_corregidos"],
        "viajes_cancelados": f["num_cancelados"],
    }


def momento_de_correccion(sesion: Sesion, correccion_id: int):
    with _conexion(sesion) as conn, conn.cursor() as cur:
        cur.execute("SELECT aplicada_en FROM correcciones WHERE id = %s", (correccion_id,))
        f = cur.fetchone()
    return f["aplicada_en"] if f else None


def resumen_en(sesion: Sesion, instante) -> dict:
    """
    Metricas tal y como estaban en 'instante', reconstruidas desde el log de
    eventos (no hay snapshots), junto a las actuales y la diferencia.
    """
    with _conexion(sesion) as conn, conn.cursor() as cur:
        cur.execute(_SQL_RESUMEN_EN, (instante,))
        antes = _fila_resumen(cur.fetchone())
        cur.execute("SELECT clock_timestamp() AS ahora")
        ahora_ts = cur.fetchone()["ahora"]
        cur.execute(_SQL_RESUMEN_EN, (ahora_ts,))
        ahora = _fila_resumen(cur.fetchone())
        cur.execute(
            """SELECT COUNT(*) AS n FROM correcciones
                WHERE aplicada_en > %s AND aplicada_en <= %s""",
            (instante, ahora_ts),
        )
        posteriores = cur.fetchone()["n"]
    return {
        "empresa": sesion.empresa_id,
        "instante": instante.isoformat(),
        "en_ese_instante": antes,
        "ahora": ahora,
        "diferencia": {k: round(ahora[k] - antes[k], 2) for k in antes},
        "correcciones_posteriores": posteriores,
        "moneda": "USD",
    }


# ---------------------------------------------------------------------
# Registro encadenado por hash (a prueba de manipulaciones)
# ---------------------------------------------------------------------

GENESIS = "0" * 64


def _num_canonico(v) -> str:
    return "" if v is None else f"{v:.2f}"


def contenido_canonico(fila: dict) -> str:
    """Misma serializacion que la funcion SQL contenido_canonico(), pero
    implementada de forma independiente: si una alteracion engañara a una,
    tendria que engañar tambien a la otra."""

    momento = fila["aplicada_en"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return "|".join([
        fila["hash_anterior"], str(fila["eslabon"]), fila["empresa_id"], str(fila["viaje_id"]),
        fila["tipo"], fila["campo"] or "", _num_canonico(fila["valor_original"]),
        _num_canonico(fila["valor_nuevo"]), fila["motivo"], fila["aplicada_por"], momento,
    ])


def verificar_eslabones(filas: list[dict]) -> dict:
    """Recorre una cadena (ordenada por eslabon) y devuelve el primer fallo."""
    import hashlib

    previo, esperado_n = GENESIS, 1
    for f in filas:
        if f["eslabon"] != esperado_n:
            return {"integra": False, "eslabon": esperado_n,
                    "problema": f"falta el eslabón {esperado_n} (borrado)"}
        if f["hash_anterior"] != previo:
            return {"integra": False, "eslabon": f["eslabon"],
                    "problema": "no enlaza con el eslabón anterior"}
        calculado = hashlib.sha256(contenido_canonico(f).encode("utf-8")).hexdigest()
        if calculado != f["hash"]:
            return {"integra": False, "eslabon": f["eslabon"],
                    "problema": "su contenido no coincide con su hash (modificado)",
                    "correccion_id": f["id"]}
        previo, esperado_n = f["hash"], esperado_n + 1
    return {"integra": True, "eslabones": len(filas), "cabeza": previo}


def verificar_cadena(sesion: Sesion) -> dict:
    """Verifica la cadena de cada empresa visible (la propia; todas para el auditor)."""
    with _conexion(sesion) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id, empresa_id, viaje_id, tipo, campo, valor_original, valor_nuevo,
                      motivo, aplicada_por, aplicada_en, eslabon, hash_anterior, hash
                 FROM correcciones ORDER BY empresa_id, eslabon"""
        )
        filas = cur.fetchall()
    por_empresa: dict[str, list] = {}
    for f in filas:
        por_empresa.setdefault(f["empresa_id"], []).append(f)
    if not sesion.puede("ver_global"):
        por_empresa.setdefault(sesion.empresa_id, [])
    cadenas = [{"empresa": e, **verificar_eslabones(fs)} for e, fs in sorted(por_empresa.items())]
    return {"integra": all(c["integra"] for c in cadenas), "cadenas": cadenas,
            "algoritmo": "SHA-256 encadenado por empresa"}


def consistencia_cubo() -> dict:
    """
    Invariante del recalculo incremental: el cubo materializado debe ser
    IDENTICO a recalcular todo desde el log de eventos. Si el disparador
    se saltara un cubo, aqui apareceria la diferencia.
    """
    with db.conexion_empresa("plataforma", "auditor") as conn, conn.cursor() as cur:
        cur.execute(
            """WITH desde_log AS (
                   SELECT ve.empresa_id, ve.pickup_ts::date AS fecha, z.borough,
                          COUNT(*) FILTER (WHERE NOT ve.cancelado) AS num_viajes,
                          COALESCE(SUM(ve.importe_total) FILTER (WHERE NOT ve.cancelado), 0) AS importe
                     FROM viajes_en(clock_timestamp()) ve
                     JOIN zonas z ON z.location_id = ve.pu_location_id
                    GROUP BY 1, 2, 3
               )
               SELECT COUNT(*) AS cubos,
                      COUNT(*) FILTER (WHERE m.empresa_id IS NULL OR l.empresa_id IS NULL
                                        OR m.num_viajes <> l.num_viajes
                                        OR m.importe_total <> l.importe) AS distintos
                 FROM metricas_diarias m
                 FULL JOIN desde_log l USING (empresa_id, fecha, borough)"""
        )
        f = cur.fetchone()
    return {"descripcion": "Cubo incremental comparado con un recálculo completo desde el log",
            "cubos_comparados": f["cubos"], "cubos_distintos": f["distintos"],
            "consistente": f["distintos"] == 0}
