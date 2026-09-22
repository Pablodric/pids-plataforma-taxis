-- =====================================================================
-- Plataforma de datos de taxis - Esquema
-- Restricciones implementadas:
--   E6. Datos corregibles     -> tabla correcciones + vista vigente
--                                + recalculo incremental de metricas
--   E7. Plataforma multiempresa -> empresa_id en cada hecho + RLS
--                                + rol de aplicacion sin privilegios
-- =====================================================================

-- ---------------------------------------------------------------------
-- E7: rol de aplicacion SIN superusuario.
-- Es imprescindible: un superusuario de Postgres ignora las politicas
-- RLS, asi que si la API se conectara como 'postgres' el aislamiento
-- entre empresas no se aplicaria realmente.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pids_app') THEN
        CREATE ROLE pids_app LOGIN PASSWORD 'pids_app_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;


-- =====================================================================
-- Catalogo de empresas y usuarios (E7)
-- =====================================================================

CREATE TABLE empresas (
    id                  TEXT PRIMARY KEY,
    nombre              TEXT NOT NULL,
    vendor_id_origen    INTEGER,          -- VendorID del dataset que le pertenece
    -- E7: "Evitar que una empresa consuma todos los recursos disponibles"
    cuota_consultas_min INTEGER NOT NULL DEFAULT 20,
    creada_en           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE usuarios (
    id          SERIAL PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    empresa_id  TEXT NOT NULL REFERENCES empresas(id),
    -- 'usuario'    -> solo datos de su empresa
    -- 'operador'   -> ademas puede registrar correcciones (E6)
    -- 'auditor'    -> ademas puede consultar metricas globales (E7)
    rol         TEXT NOT NULL CHECK (rol IN ('usuario', 'operador', 'auditor')),
    -- Guardamos hash, nunca la contrasena en claro
    password_hash TEXT NOT NULL,
    activo      BOOLEAN NOT NULL DEFAULT TRUE
);


-- =====================================================================
-- Datos de referencia compartidos (no pertenecen a ninguna empresa)
-- Fuente externa: NYC TLC taxi zone lookup (265 zonas)
-- =====================================================================

CREATE TABLE zonas (
    location_id  INTEGER PRIMARY KEY,
    borough      TEXT NOT NULL,
    zona         TEXT NOT NULL,
    service_zone TEXT
);

CREATE INDEX idx_zonas_borough ON zonas (borough);


-- =====================================================================
-- E6: los viajes ingeridos son HECHOS INMUTABLES.
-- Una correccion nunca sobrescribe esta tabla: se anota aparte.
-- E7: cada viaje lleva su empresa propietaria desde la ingesta.
-- =====================================================================

CREATE TABLE viajes (
    id                  BIGSERIAL PRIMARY KEY,
    empresa_id          TEXT NOT NULL REFERENCES empresas(id),
    vendor_id           INTEGER,
    pickup_ts           TIMESTAMP NOT NULL,
    dropoff_ts          TIMESTAMP,
    pasajeros           INTEGER,
    distancia           NUMERIC(8,2),
    pu_location_id      INTEGER REFERENCES zonas(location_id),
    do_location_id      INTEGER REFERENCES zonas(location_id),
    tipo_pago           INTEGER,
    importe_base        NUMERIC(10,2),
    propina             NUMERIC(10,2),
    peajes              NUMERIC(10,2),
    recargo_congestion  NUMERIC(10,2),
    importe_total       NUMERIC(10,2),
    ingerido_en         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_viajes_empresa_fecha ON viajes (empresa_id, (pickup_ts::date));
CREATE INDEX idx_viajes_pu            ON viajes (pu_location_id);


-- =====================================================================
-- E6: historial de correcciones y cancelaciones.
-- "Conservar el valor original, la correccion y el momento en que se aplico"
-- Cada fila es un evento; nunca se borra ni se modifica.
-- =====================================================================

CREATE TABLE correcciones (
    id              BIGSERIAL PRIMARY KEY,
    viaje_id        BIGINT NOT NULL REFERENCES viajes(id),
    empresa_id      TEXT NOT NULL REFERENCES empresas(id),
    tipo            TEXT NOT NULL CHECK (tipo IN ('correccion', 'cancelacion')),
    -- Para 'correccion': que campo se corrige. Para 'cancelacion': NULL.
    campo           TEXT CHECK (campo IS NULL OR campo IN
                        ('importe_total', 'distancia', 'pasajeros', 'propina')),
    valor_original  NUMERIC(10,2),
    valor_nuevo     NUMERIC(10,2),
    motivo          TEXT,
    aplicada_en     TIMESTAMPTZ NOT NULL DEFAULT now(),
    aplicada_por    TEXT NOT NULL
);

CREATE INDEX idx_correcciones_viaje   ON correcciones (viaje_id);
CREATE INDEX idx_correcciones_empresa ON correcciones (empresa_id, aplicada_en DESC);


-- =====================================================================
-- E6: vista de estado VIGENTE.
-- Aplica la ultima correccion de cada campo y marca los cancelados.
-- Las metricas leen de aqui; el historico completo sigue intacto en
-- 'viajes' + 'correcciones'.
-- =====================================================================

-- security_invoker = true es IMPRESCINDIBLE aqui (Postgres 15+).
-- Por defecto una vista se ejecuta con los permisos de su propietario
-- (postgres), que se salta las politicas RLS de las tablas de debajo:
-- sin esta opcion, la vista devolveria viajes de TODAS las empresas y
-- el aislamiento de E7 quedaria roto por una puerta lateral.
CREATE VIEW v_viajes_vigentes WITH (security_invoker = true) AS
WITH ultima_correccion AS (
    SELECT DISTINCT ON (viaje_id, campo)
           viaje_id, campo, valor_nuevo, aplicada_en
      FROM correcciones
     WHERE tipo = 'correccion'
     ORDER BY viaje_id, campo, aplicada_en DESC
),
cancelados AS (
    SELECT DISTINCT viaje_id FROM correcciones WHERE tipo = 'cancelacion'
)
SELECT
    v.id,
    v.empresa_id,
    v.pickup_ts,
    v.dropoff_ts,
    v.pu_location_id,
    v.do_location_id,
    v.tipo_pago,
    COALESCE((SELECT valor_nuevo FROM ultima_correccion u
               WHERE u.viaje_id = v.id AND u.campo = 'pasajeros'), v.pasajeros)         AS pasajeros,
    COALESCE((SELECT valor_nuevo FROM ultima_correccion u
               WHERE u.viaje_id = v.id AND u.campo = 'distancia'), v.distancia)         AS distancia,
    COALESCE((SELECT valor_nuevo FROM ultima_correccion u
               WHERE u.viaje_id = v.id AND u.campo = 'propina'), v.propina)             AS propina,
    COALESCE((SELECT valor_nuevo FROM ultima_correccion u
               WHERE u.viaje_id = v.id AND u.campo = 'importe_total'), v.importe_total) AS importe_total,
    v.importe_total AS importe_total_original,
    -- Trazabilidad: estas dos columnas permiten al chatbot avisar de que
    -- una metrica incluye datos corregidos (requisito explicito de E6).
    EXISTS (SELECT 1 FROM correcciones c
             WHERE c.viaje_id = v.id AND c.tipo = 'correccion')   AS tiene_correccion,
    (v.id IN (SELECT viaje_id FROM cancelados))                   AS cancelado
FROM viajes v;


-- =====================================================================
-- E6: agregado materializado con RECALCULO INCREMENTAL.
-- "Actualizar las metricas afectadas sin recalcular innecesariamente
--  todo el historico" -> la unidad de recalculo es (empresa, fecha, borough).
-- =====================================================================

CREATE TABLE metricas_diarias (
    empresa_id         TEXT NOT NULL REFERENCES empresas(id),
    fecha              DATE NOT NULL,
    borough            TEXT NOT NULL,
    num_viajes         INTEGER NOT NULL,
    num_cancelados     INTEGER NOT NULL DEFAULT 0,
    num_corregidos     INTEGER NOT NULL DEFAULT 0,
    importe_total      NUMERIC(14,2),
    importe_medio      NUMERIC(10,2),
    distancia_media    NUMERIC(10,2),
    propina_media      NUMERIC(10,2),
    -- clock_timestamp() y no now(): now() devuelve el instante de INICIO de
    -- la transaccion, asi que el recalculo disparado por una correccion
    -- tendria exactamente su misma marca de tiempo y la latencia de
    -- propagacion medida saldria siempre 0.
    recalculada_en     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (empresa_id, fecha, borough)
);


-- Recalcula UN solo cubo (empresa, fecha, borough).
CREATE OR REPLACE FUNCTION recalcular_cubo(
    p_empresa TEXT, p_fecha DATE, p_borough TEXT
) RETURNS VOID AS $$
BEGIN
    DELETE FROM metricas_diarias
     WHERE empresa_id = p_empresa AND fecha = p_fecha AND borough = p_borough;

    INSERT INTO metricas_diarias (
        empresa_id, fecha, borough, num_viajes, num_cancelados,
        num_corregidos, importe_total, importe_medio, distancia_media,
        propina_media, recalculada_en)
    SELECT
        p_empresa,
        p_fecha,
        p_borough,
        COUNT(*) FILTER (WHERE NOT vv.cancelado),
        COUNT(*) FILTER (WHERE vv.cancelado),
        COUNT(*) FILTER (WHERE vv.tiene_correccion AND NOT vv.cancelado),
        COALESCE(SUM(vv.importe_total) FILTER (WHERE NOT vv.cancelado), 0),
        AVG(vv.importe_total) FILTER (WHERE NOT vv.cancelado),
        AVG(vv.distancia)     FILTER (WHERE NOT vv.cancelado),
        AVG(vv.propina)       FILTER (WHERE NOT vv.cancelado),
        clock_timestamp()
      FROM v_viajes_vigentes vv
      JOIN zonas z ON z.location_id = vv.pu_location_id
     WHERE vv.empresa_id = p_empresa
       AND vv.pickup_ts::date = p_fecha
       AND z.borough = p_borough
    HAVING COUNT(*) > 0;
END;
$$ LANGUAGE plpgsql;


-- Recalculo completo: solo se usa en la carga inicial.
CREATE OR REPLACE FUNCTION recalcular_todo() RETURNS INTEGER AS $$
DECLARE
    r RECORD;
    n INTEGER := 0;
BEGIN
    TRUNCATE metricas_diarias;
    FOR r IN
        SELECT DISTINCT vv.empresa_id, vv.pickup_ts::date AS fecha, z.borough
          FROM v_viajes_vigentes vv
          JOIN zonas z ON z.location_id = vv.pu_location_id
    LOOP
        PERFORM recalcular_cubo(r.empresa_id, r.fecha, r.borough);
        n := n + 1;
    END LOOP;
    RETURN n;
END;
$$ LANGUAGE plpgsql;


-- E6: al registrar una correccion, recalcular SOLO el cubo afectado.
-- Es lo que evita recorrer todo el historico en cada cambio.
CREATE OR REPLACE FUNCTION trg_correccion_recalcula() RETURNS TRIGGER AS $$
DECLARE
    v_fecha   DATE;
    v_borough TEXT;
BEGIN
    SELECT v.pickup_ts::date, z.borough
      INTO v_fecha, v_borough
      FROM viajes v
      JOIN zonas z ON z.location_id = v.pu_location_id
     WHERE v.id = NEW.viaje_id;

    IF v_fecha IS NOT NULL THEN
        PERFORM recalcular_cubo(NEW.empresa_id, v_fecha, v_borough);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER correccion_recalcula
    AFTER INSERT ON correcciones
    FOR EACH ROW EXECUTE FUNCTION trg_correccion_recalcula();


-- =====================================================================
-- Auditoria de accesos: alimenta la metrica de calidad "aislamiento"
-- =====================================================================

CREATE TABLE auditoria_accesos (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    usuario     TEXT,
    empresa_id  TEXT,
    accion      TEXT NOT NULL,
    detalle     TEXT,
    permitido   BOOLEAN NOT NULL,
    latencia_ms INTEGER
);

CREATE INDEX idx_auditoria_ts ON auditoria_accesos (ts DESC);


-- =====================================================================
-- E7: Row Level Security.
-- Segunda linea de defensa: aunque una consulta de la API olvidara
-- filtrar por empresa, Postgres no devuelve filas de otras empresas.
-- La API fija app.empresa_id en cada transaccion a partir del JWT.
-- =====================================================================

ALTER TABLE viajes            ENABLE ROW LEVEL SECURITY;
ALTER TABLE correcciones      ENABLE ROW LEVEL SECURITY;
ALTER TABLE metricas_diarias  ENABLE ROW LEVEL SECURITY;

-- Un usuario normal solo ve su empresa.
-- Un auditor (app.rol = 'auditor') puede ver el conjunto: es la excepcion
-- controlada que pide E7 ("metricas globales solo para roles autorizados").
-- CUIDADO: varias politicas permisivas sobre la misma tabla y operacion
-- se combinan con OR. Una politica de escritura con USING (TRUE) anularia
-- el filtro de lectura. Por eso cada politica se acota a su operacion y
-- ninguna es incondicional.

-- Viajes: solo lectura para la aplicacion.
CREATE POLICY p_viajes_select ON viajes
    FOR SELECT
    USING (empresa_id = current_setting('app.empresa_id', TRUE)
           OR current_setting('app.rol', TRUE) = 'auditor');

-- Correcciones: se leen con el mismo criterio y solo se pueden insertar
-- a nombre de la propia empresa (ni siquiera el auditor escribe aqui).
CREATE POLICY p_correcciones_select ON correcciones
    FOR SELECT
    USING (empresa_id = current_setting('app.empresa_id', TRUE)
           OR current_setting('app.rol', TRUE) = 'auditor');

CREATE POLICY p_correcciones_insert ON correcciones
    FOR INSERT
    WITH CHECK (empresa_id = current_setting('app.empresa_id', TRUE));

-- Metricas: lectura con el criterio de empresa; escritura acotada a la
-- propia empresa, que es lo unico que necesita el recalculo incremental.
CREATE POLICY p_metricas_select ON metricas_diarias
    FOR SELECT
    USING (empresa_id = current_setting('app.empresa_id', TRUE)
           OR current_setting('app.rol', TRUE) = 'auditor');

CREATE POLICY p_metricas_insert ON metricas_diarias
    FOR INSERT
    WITH CHECK (empresa_id = current_setting('app.empresa_id', TRUE));

CREATE POLICY p_metricas_delete ON metricas_diarias
    FOR DELETE
    USING (empresa_id = current_setting('app.empresa_id', TRUE));


-- Permisos del rol de aplicacion
GRANT USAGE ON SCHEMA public TO pids_app;
GRANT SELECT ON zonas, empresas, usuarios, v_viajes_vigentes TO pids_app;
GRANT SELECT ON viajes TO pids_app;
GRANT SELECT, INSERT ON correcciones TO pids_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON metricas_diarias TO pids_app;
GRANT SELECT, INSERT ON auditoria_accesos TO pids_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pids_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO pids_app;
