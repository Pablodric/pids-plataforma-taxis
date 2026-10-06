-- =====================================================================
-- Plataforma de datos de taxis - Esquema
-- Restricciones implementadas:
--   E6. Datos corregibles     -> tabla correcciones + vista vigente
--                                + recalculo incremental de metricas
--   E7. Plataforma multiempresa -> empresa_id en cada hecho + RLS
--                                + rol de aplicacion sin privilegios
--                                + FK compuesta viaje/empresa
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
    cuota_consultas_min INTEGER NOT NULL DEFAULT 20 CHECK (cuota_consultas_min > 0),
    creada_en           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE usuarios (
    id          SERIAL PRIMARY KEY,
    email       TEXT UNIQUE NOT NULL,
    empresa_id  TEXT NOT NULL REFERENCES empresas(id),
    -- 'usuario'    -> solo consulta datos de su empresa
    -- 'operador'   -> ademas puede registrar correcciones (E6)
    -- 'auditor'    -> consulta metricas globales (E7), pero NO escribe:
    --                 quien audita no debe poder alterar lo auditado
    -- 'proveedor'  -> cuenta de maquina de la empresa: SOLO envia viajes
    --                 nuevos (ingesta en tiempo real); no consulta ni corrige
    rol         TEXT NOT NULL CHECK (rol IN ('usuario', 'operador', 'auditor', 'proveedor')),
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
-- Registro de cada ejecucion de la ingesta: fichero, huella SHA-256 y
-- recuento de filas cargadas, descartadas y anomalas. Permite saber de
-- donde sale cada cifra (base para E5 si hubiera que adaptarse).
-- =====================================================================

CREATE TABLE ingestas (
    id                SERIAL PRIMARY KEY,
    fichero           TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    filas_leidas      INTEGER NOT NULL,
    filas_cargadas    INTEGER NOT NULL,
    filas_descartadas INTEGER NOT NULL,
    anomalias         JSONB NOT NULL DEFAULT '{}'::jsonb,
    ejecutada_en      TIMESTAMPTZ NOT NULL DEFAULT now()
);


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
    ingerido_en         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Necesaria para la FK compuesta de 'correcciones' (ver abajo)
    CONSTRAINT uq_viajes_id_empresa UNIQUE (id, empresa_id)
);

CREATE INDEX idx_viajes_empresa_fecha ON viajes (empresa_id, (pickup_ts::date));
CREATE INDEX idx_viajes_pu            ON viajes (pu_location_id);
CREATE INDEX idx_viajes_pickup        ON viajes (pickup_ts DESC);


-- =====================================================================
-- E6: historial de correcciones y cancelaciones.
-- "Conservar el valor original, la correccion y el momento en que se aplico"
-- Cada fila es un evento; nunca se borra ni se modifica.
-- =====================================================================

CREATE TABLE correcciones (
    id              BIGSERIAL PRIMARY KEY,
    viaje_id        BIGINT NOT NULL,
    empresa_id      TEXT NOT NULL REFERENCES empresas(id),
    tipo            TEXT NOT NULL CHECK (tipo IN ('correccion', 'cancelacion')),
    -- Para 'correccion': que campo se corrige. Para 'cancelacion': NULL.
    campo           TEXT CHECK (campo IS NULL OR campo IN
                        ('importe_total', 'distancia', 'pasajeros', 'propina')),
    valor_original  NUMERIC(10,2),
    valor_nuevo     NUMERIC(10,2),
    motivo          TEXT NOT NULL CHECK (length(btrim(motivo)) >= 3),
    aplicada_en     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    aplicada_por    TEXT NOT NULL,

    -- Registro a prueba de manipulaciones: cada correccion guarda el hash
    -- SHA-256 de su contenido y del hash de la anterior de SU empresa
    -- (una cadena por empresa, como un libro contable). Si alguien con
    -- acceso de superusuario altera o borra un eslabon, la cadena deja de
    -- cuadrar y la verificacion (GET /auditoria/cadena) lo detecta.
    eslabon         BIGINT NOT NULL,
    hash_anterior   CHAR(64) NOT NULL,
    hash            CHAR(64) NOT NULL,
    CONSTRAINT uq_eslabon_por_empresa UNIQUE (empresa_id, eslabon),

    -- E7: la empresa de la correccion TIENE que ser la del viaje.
    -- Con una FK simple sobre viaje_id, un rol que pudiera leer viajes
    -- ajenos (el auditor) podia anotar una correccion con su propia
    -- empresa sobre el viaje de otra. La FK compuesta lo hace imposible
    -- en el propio motor, se llame desde donde se llame.
    CONSTRAINT fk_correccion_viaje_empresa
        FOREIGN KEY (viaje_id, empresa_id) REFERENCES viajes (id, empresa_id),

    -- Coherencia del evento segun su tipo
    CONSTRAINT ck_forma_evento CHECK (
        (tipo = 'correccion'  AND campo IS NOT NULL AND valor_nuevo IS NOT NULL)
     OR (tipo = 'cancelacion' AND campo IS NULL     AND valor_nuevo IS NULL)
    )
);

CREATE INDEX idx_correcciones_viaje   ON correcciones (viaje_id, aplicada_en DESC, id DESC);
CREATE INDEX idx_correcciones_empresa ON correcciones (empresa_id, aplicada_en DESC);

-- E6: un viaje solo se puede cancelar una vez. Comprobarlo en la API no
-- basta: dos peticiones simultaneas pasarian las dos la comprobacion.
CREATE UNIQUE INDEX uq_una_cancelacion_por_viaje
    ON correcciones (viaje_id) WHERE tipo = 'cancelacion';


-- ---------------------------------------------------------------------
-- Cadena de hashes. El contenido canonico se recalcula de forma
-- independiente en Python al verificar (repositorio.verificar_cadena):
-- dos implementaciones distintas tienen que coincidir byte a byte.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION contenido_canonico(
    p_hash_anterior TEXT, p_eslabon BIGINT, p_empresa TEXT, p_viaje BIGINT,
    p_tipo TEXT, p_campo TEXT, p_original NUMERIC, p_nuevo NUMERIC,
    p_motivo TEXT, p_autor TEXT, p_momento TIMESTAMPTZ
) RETURNS TEXT AS $$
    SELECT concat_ws('|', p_hash_anterior, p_eslabon, p_empresa, p_viaje, p_tipo,
                     coalesce(p_campo, ''), coalesce(p_original::text, ''),
                     coalesce(p_nuevo::text, ''), p_motivo, p_autor,
                     to_char(p_momento AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'))
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION trg_encadenar() RETURNS TRIGGER AS $$
DECLARE
    v_prev    TEXT;
    v_eslabon BIGINT;
BEGIN
    -- Serializa las inserciones de UNA empresa (las demas no esperan)
    PERFORM pg_advisory_xact_lock(hashtext('cadena:' || NEW.empresa_id));
    SELECT hash, eslabon INTO v_prev, v_eslabon
      FROM correcciones
     WHERE empresa_id = NEW.empresa_id
     ORDER BY eslabon DESC
     LIMIT 1;
    NEW.eslabon       := COALESCE(v_eslabon, 0) + 1;
    NEW.hash_anterior := COALESCE(v_prev, repeat('0', 64));
    NEW.hash := encode(sha256(convert_to(contenido_canonico(
        NEW.hash_anterior, NEW.eslabon, NEW.empresa_id, NEW.viaje_id, NEW.tipo,
        NEW.campo, NEW.valor_original, NEW.valor_nuevo, NEW.motivo,
        NEW.aplicada_por, NEW.aplicada_en), 'UTF8')), 'hex');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER correccion_encadena
    BEFORE INSERT ON correcciones
    FOR EACH ROW EXECUTE FUNCTION trg_encadenar();


-- ---------------------------------------------------------------------
-- E6: registro de solo-anadir. Ni siquiera el superusuario puede editar
-- o borrar un hecho o una correccion ya registrados: el historial es la
-- prueba de lo ocurrido. (Para empezar de cero: docker compose down -v)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION trg_solo_anadir() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'La tabla % es de solo-anadir: registra una correccion en lugar de modificarla', TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER viajes_inmutables
    BEFORE UPDATE OR DELETE ON viajes
    FOR EACH ROW EXECUTE FUNCTION trg_solo_anadir();

CREATE TRIGGER correcciones_inmutables
    BEFORE UPDATE OR DELETE ON correcciones
    FOR EACH ROW EXECUTE FUNCTION trg_solo_anadir();


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
--
-- Las correcciones de cada viaje se resuelven en un unico LATERAL que
-- usa el indice (viaje_id, aplicada_en DESC): coste proporcional a las
-- correcciones de ESE viaje, no a toda la tabla de correcciones.
CREATE VIEW v_viajes_vigentes WITH (security_invoker = true) AS
SELECT
    v.id,
    v.empresa_id,
    v.pickup_ts,
    v.dropoff_ts,
    v.pu_location_id,
    v.do_location_id,
    v.tipo_pago,
    COALESCE(c.pasajeros,     v.pasajeros)     AS pasajeros,
    COALESCE(c.distancia,     v.distancia)     AS distancia,
    COALESCE(c.propina,       v.propina)       AS propina,
    COALESCE(c.importe_total, v.importe_total) AS importe_total,
    v.pasajeros     AS pasajeros_original,
    v.distancia     AS distancia_original,
    v.propina       AS propina_original,
    v.importe_total AS importe_total_original,
    -- Trazabilidad: estas columnas permiten al chatbot avisar de que
    -- una metrica incluye datos corregidos (requisito explicito de E6).
    COALESCE(c.num_correcciones, 0)            AS num_correcciones,
    COALESCE(c.num_correcciones, 0) > 0        AS tiene_correccion,
    COALESCE(c.cancelado, FALSE)               AS cancelado
FROM viajes v
LEFT JOIN LATERAL (
    SELECT
        (array_agg(cc.valor_nuevo ORDER BY cc.aplicada_en DESC, cc.id DESC)
            FILTER (WHERE cc.campo = 'pasajeros'))[1]     AS pasajeros,
        (array_agg(cc.valor_nuevo ORDER BY cc.aplicada_en DESC, cc.id DESC)
            FILTER (WHERE cc.campo = 'distancia'))[1]     AS distancia,
        (array_agg(cc.valor_nuevo ORDER BY cc.aplicada_en DESC, cc.id DESC)
            FILTER (WHERE cc.campo = 'propina'))[1]       AS propina,
        (array_agg(cc.valor_nuevo ORDER BY cc.aplicada_en DESC, cc.id DESC)
            FILTER (WHERE cc.campo = 'importe_total'))[1] AS importe_total,
        COUNT(*) FILTER (WHERE cc.tipo = 'correccion')     AS num_correcciones,
        bool_or(cc.tipo = 'cancelacion')                   AS cancelado
      FROM correcciones cc
     WHERE cc.viaje_id = v.id
) c ON TRUE;


-- =====================================================================
-- E6: VIAJE EN EL TIEMPO. Como el historico es un log de eventos que
-- nunca se modifica, el estado de los datos en CUALQUIER instante pasado
-- se puede reconstruir: basta con aplicar solo las correcciones hechas
-- hasta ese momento. Responde a "¿cuanto facturabamos antes de que el
-- proveedor corrigiera el viaje 411?" sin guardar copias ni snapshots.
--
-- Funcion SQL con permisos del invocador: el RLS se aplica igual que en
-- la vista vigente (cada empresa solo reconstruye su propio pasado).
-- =====================================================================

CREATE OR REPLACE FUNCTION viajes_en(p_instante TIMESTAMPTZ)
RETURNS TABLE (
    id BIGINT, empresa_id TEXT, pickup_ts TIMESTAMP, pu_location_id INTEGER,
    tipo_pago INTEGER, pasajeros NUMERIC, distancia NUMERIC, propina NUMERIC,
    importe_total NUMERIC, tiene_correccion BOOLEAN, cancelado BOOLEAN
) AS $$
    SELECT v.id, v.empresa_id, v.pickup_ts, v.pu_location_id, v.tipo_pago,
           COALESCE(c.pasajeros, v.pasajeros), COALESCE(c.distancia, v.distancia),
           COALESCE(c.propina, v.propina), COALESCE(c.importe_total, v.importe_total),
           COALESCE(c.num_correcciones, 0) > 0, COALESCE(c.cancelado, FALSE)
      FROM viajes v
      LEFT JOIN LATERAL (
          SELECT
              (array_agg(cc.valor_nuevo ORDER BY cc.aplicada_en DESC, cc.id DESC)
                  FILTER (WHERE cc.campo = 'pasajeros'))[1]     AS pasajeros,
              (array_agg(cc.valor_nuevo ORDER BY cc.aplicada_en DESC, cc.id DESC)
                  FILTER (WHERE cc.campo = 'distancia'))[1]     AS distancia,
              (array_agg(cc.valor_nuevo ORDER BY cc.aplicada_en DESC, cc.id DESC)
                  FILTER (WHERE cc.campo = 'propina'))[1]       AS propina,
              (array_agg(cc.valor_nuevo ORDER BY cc.aplicada_en DESC, cc.id DESC)
                  FILTER (WHERE cc.campo = 'importe_total'))[1] AS importe_total,
              COUNT(*) FILTER (WHERE cc.tipo = 'correccion')     AS num_correcciones,
              bool_or(cc.tipo = 'cancelacion')                   AS cancelado
            FROM correcciones cc
           WHERE cc.viaje_id = v.id AND cc.aplicada_en <= p_instante
      ) c ON TRUE
     WHERE v.ingerido_en <= p_instante
$$ LANGUAGE sql STABLE;


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


-- Ingesta en tiempo real: cuando entra un viaje nuevo se recalcula SOLO
-- su cubo, igual que con una correccion. La carga inicial del CSV fija
-- app.carga_masiva = '1' para saltarse este paso fila a fila y calcular
-- todos los cubos una unica vez al final (recalcular_todo).
CREATE OR REPLACE FUNCTION trg_viaje_recalcula() RETURNS TRIGGER AS $$
DECLARE
    v_borough TEXT;
BEGIN
    IF current_setting('app.carga_masiva', TRUE) = '1' THEN
        RETURN NEW;
    END IF;
    SELECT z.borough INTO v_borough FROM zonas z WHERE z.location_id = NEW.pu_location_id;
    IF v_borough IS NOT NULL THEN
        PERFORM recalcular_cubo(NEW.empresa_id, NEW.pickup_ts::date, v_borough);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER viaje_recalcula
    AFTER INSERT ON viajes
    FOR EACH ROW EXECUTE FUNCTION trg_viaje_recalcula();


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
-- La API fija app.empresa_id y app.rol en cada transaccion a partir
-- del JWT.
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

-- E7: un viaje nuevo solo puede entrar a nombre de la empresa de la sesion,
-- y solo desde su cuenta de proveedor. Aunque la API tuviera un fallo, una
-- empresa no puede insertar viajes a nombre de otra.
CREATE POLICY p_viajes_insert ON viajes
    FOR INSERT
    WITH CHECK (empresa_id = current_setting('app.empresa_id', TRUE)
                AND current_setting('app.rol', TRUE) = 'proveedor');

-- Correcciones: se leen con el mismo criterio. Solo un OPERADOR puede
-- insertarlas, y solo a nombre de su propia empresa: el auditor lee
-- todo pero no escribe nada (tercera barrera, tras la API y la FK).
CREATE POLICY p_correcciones_select ON correcciones
    FOR SELECT
    USING (empresa_id = current_setting('app.empresa_id', TRUE)
           OR current_setting('app.rol', TRUE) = 'auditor');

CREATE POLICY p_correcciones_insert ON correcciones
    FOR INSERT
    WITH CHECK (empresa_id = current_setting('app.empresa_id', TRUE)
                AND current_setting('app.rol', TRUE) = 'operador');

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


-- Permisos del rol de aplicacion (minimos: sin UPDATE ni DELETE sobre
-- hechos ni correcciones)
GRANT USAGE ON SCHEMA public TO pids_app;
GRANT SELECT ON zonas, empresas, usuarios, ingestas, v_viajes_vigentes TO pids_app;
GRANT SELECT, INSERT ON viajes TO pids_app;
GRANT SELECT, INSERT ON correcciones TO pids_app;
GRANT SELECT, INSERT, DELETE ON metricas_diarias TO pids_app;
GRANT SELECT, INSERT ON auditoria_accesos TO pids_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO pids_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO pids_app;
