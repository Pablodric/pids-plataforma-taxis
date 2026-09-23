# ADR 0001 · Tabla compartida + Row Level Security

**Contexto.** E7 exige que cada empresa vea solo sus datos compartiendo
infraestructura, sin una instalación por empresa, y con métricas globales para
un rol autorizado.

**Decisión.** Una sola base de datos y tablas compartidas con `empresa_id` en
cada hecho, y políticas RLS de Postgres que filtran por `app.empresa_id`. La
API fija ese valor en cada transacción a partir del JWT. La API se conecta con
un rol **sin superusuario** (`pids_app`), porque un superusuario ignora el RLS.

**Alternativas descartadas.**
- *Una base de datos por empresa*: es lo que el enunciado prohíbe.
- *Un esquema por empresa*: las métricas globales exigirían consultar N esquemas.
- *Solo `WHERE` en la API*: un filtro olvidado filtra datos a otro cliente.

**Consecuencias.**
- (+) Un fallo en la API no basta para filtrar datos: el motor es la última barrera.
- (−) Hay que recordar `security_invoker` en las vistas: la primera versión lo
  olvidó y la prueba `test_el_detalle_de_viajes_tampoco_se_cruza` lo detectó.
- (−) Las funciones SQL que leen datos deben ser `SECURITY INVOKER` (lo son).
