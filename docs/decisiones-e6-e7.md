# Decisiones técnicas justificadas por las restricciones

Este documento recorre requisito a requisito lo que pide el enunciado para E6 y
E7, dónde está resuelto en el código y por qué se eligió esa opción frente a las
alternativas que se valoraron.

---

## E6. Datos corregibles

> *Los proveedores pueden corregir o cancelar viajes después de que hayan sido
> ingeridos y procesados. Las métricas deben reflejar la corrección sin perder
> el historial de lo ocurrido.*

### Requisito 1 — Representar la corrección como parte del flujo de datos

**Decisión:** una corrección es un **evento nuevo** en la tabla `correcciones`,
nunca un `UPDATE` sobre `viajes`.

| Alternativa | Por qué se descartó |
|---|---|
| `UPDATE` directo sobre el viaje | Destruye el valor anterior. Incumple el requisito de conservar el original. |
| Versionado de filas (`viajes_v1`, `viajes_v2`) | Duplica el hecho completo para cambiar un campo; las consultas se complican con `MAX(version)` por todas partes. |
| **Log de correcciones (elegida)** | El hecho original queda intacto, cada cambio es una fila con su momento, y el estado actual se deriva con una vista. |

La tabla `viajes` queda así como un registro de hechos inmutable, y
`correcciones` como el flujo de cambios sobre ellos. Es el mismo principio que
usan los sistemas contables: no se borra un apunte, se hace un asiento de
corrección.

Que sea de solo-añadir no depende de la disciplina del código: el rol de la
aplicación no tiene permisos de `UPDATE` ni `DELETE` sobre esas tablas, y un
disparador rechaza cualquier modificación o borrado incluso al superusuario.

**Dónde:** `db/init/01_esquema.sql`, tablas `viajes` y `correcciones`.

### Requisito 2 — Actualizar las métricas afectadas sin recalcular todo

**Decisión:** las métricas viven en `metricas_diarias`, un agregado
materializado cuya unidad de recálculo es el cubo
**(empresa, fecha, distrito)**. Un disparador en `correcciones` invoca
`recalcular_cubo()` solo para el cubo al que pertenece el viaje corregido.

| Alternativa | Por qué se descartó |
|---|---|
| Calcular las métricas al vuelo en cada consulta | Sencillo, pero con el histórico completo (millones de viajes) cada pregunta del chatbot recorrería toda la tabla. |
| Vista materializada de Postgres con `REFRESH` | `REFRESH MATERIALIZED VIEW` recalcula **todo**, que es justo lo que el requisito prohíbe. No admite refresco parcial. |
| **Cubo propio + disparador (elegida)** | Recalcula únicamente lo que ha cambiado, y la granularidad elegida coincide con la de las consultas del chatbot. |

**Medido:** con 999 viajes repartidos en 12 cubos, una corrección recalcula
**1 cubo de 12**. La proporción es lo relevante: con un histórico de un año, una
corrección tocaría 1 cubo de varios miles.

**Dónde:** función `recalcular_cubo()` y disparador `correccion_recalcula`.

### Requisito 3 — Conservar valor original, corrección y momento

**Decisión:** cada fila de `correcciones` guarda `valor_original`,
`valor_nuevo`, `motivo`, `aplicada_por` y `aplicada_en`.

Un matiz de diseño: `valor_original` no es el valor tal y como se ingirió, sino
**el que estaba vigente en el momento de corregir**. Así, si un viaje se corrige
dos veces, el historial es una cadena legible (29,50 → 41,84 → 91,84) que
explica cada salto por separado. El valor tal y como entró sigue disponible
siempre en la tabla `viajes`, y el panel lo muestra como *importe original*.

Para que la cadena no se rompa con dos correcciones simultáneas del mismo
viaje, cada corrección toma un bloqueo consultivo de Postgres sobre ese viaje
(`pg_advisory_xact_lock`) antes de leer el valor vigente.

**Dónde:** `repositorio.registrar_correccion()`.

### Requisito 4 — Hacer visible en el chatbot si una métrica incluye correcciones

**Decisión:** tres mecanismos encadenados.

1. La vista `v_viajes_vigentes` expone las columnas `tiene_correccion` y
   `cancelado`.
2. Toda respuesta del repositorio incluye un campo `aviso_correcciones` con el
   texto ya redactado cuando procede.
3. Las instrucciones del sistema del LLM le obligan a mencionar ese aviso; el
   motor de reglas lo añade con una plantilla; y el panel web lo resalta en
   amarillo.

Además existe la herramienta `historial_correcciones`, que responde
específicamente a *"¿por qué ha cambiado esta cifra?"* — que es el caso de uso
que el enunciado describe en la última frase de E6.

---

## E7. Plataforma multiempresa

> *Cada empresa solo puede consultar sus propios datos, métricas y
> configuración, aunque compartan la infraestructura.*

### Requisito 1 — Identificar la empresa propietaria desde la ingesta

**Decisión:** ningún hecho entra en la plataforma sin dueño. La ingesta asigna
`empresa_id` a partir del `VendorID` del dataset, y las filas sin empresa
identificable se descartan y se registran.

En el dataset público, `VendorID` identifica al proveedor tecnológico del taxi,
lo que lo convierte en el equivalente natural a la empresa. En un despliegue
real vendría del contrato de ingesta de cada empresa (una cabecera firmada, un
topic propio de Kafka, un bucket separado).

**Dónde:** `ingest/ingesta.py`, función `cargar_viajes()`.

### Requisito 2 — Aislar los datos en el almacenamiento y en la API

**Decisión:** aislamiento en **dos capas independientes**, para que un fallo en
una no exponga los datos.

1. **En la API:** `empresa_id` no es nunca un parámetro que el usuario o el
   modelo puedan elegir. Sale del JWT firmado y se inyecta en el servidor.
2. **En el motor de base de datos:** políticas *Row Level Security* sobre
   `viajes`, `correcciones` y `metricas_diarias`. La API abre cada transacción
   fijando `app.empresa_id`, y Postgres filtra aunque el SQL se olvide del
   `WHERE`.

| Alternativa | Por qué se descartó |
|---|---|
| Una base de datos por empresa | Aislamiento perfecto, pero es justo lo que el escenario prohíbe: *"sin desplegar una instalación completa para cada una"*. |
| Un esquema por empresa | Mejor que lo anterior, pero las métricas globales del auditor exigirían consultar N esquemas y unirlos a mano. |
| Solo filtrar en el código de la API | Un único `WHERE` olvidado filtra datos entre clientes. Sin red de seguridad. |
| **Tabla compartida + RLS (elegida)** | Una sola instalación, métricas globales triviales, y el motor como última línea de defensa. |

**Dos detalles que costaron, y que son el motivo de que existan pruebas:**

- **La API se conecta como `pids_app`, un rol sin superusuario.** Un
  superusuario de Postgres **ignora las políticas RLS**. Si la API se conectara
  como `postgres`, el aislamiento sería decorativo.
- **La vista lleva `WITH (security_invoker = true)`.** Por defecto, una vista se
  ejecuta con los permisos de su propietario y se salta el RLS de las tablas de
  debajo. La primera versión de este proyecto tenía ese fallo: el cubo de
  métricas aislaba correctamente, pero las consultas de detalle devolvían viajes
  de las dos empresas. Lo detectó la prueba
  `test_el_detalle_de_viajes_tampoco_se_cruza`, que sigue en la batería como
  prueba de regresión.

- **La FK de `correcciones` es compuesta: `(viaje_id, empresa_id)`.** Con una FK
  solo sobre `viaje_id`, un rol que puede leer viajes ajenos (el auditor) podía
  anotar una corrección con su propia empresa sobre el viaje de otra, y la
  política RLS de inserción no lo veía, porque solo comprueba la empresa de la
  fila nueva. La FK compuesta lo hace imposible en el motor. Lo cubre la prueba
  `test_la_bd_impide_corregir_un_viaje_ajeno_aunque_falle_la_api`, que inserta
  por SQL directo saltándose la API.

### Requisito 3 — Impedir que un usuario consulte datos de otra empresa

**Decisión:** tres barreras, de fuera hacia dentro.

1. El catálogo de herramientas que ve el modelo **no tiene ningún parámetro de
   empresa**. No existe la petición que habría que denegar.
2. Si el usuario nombra a otra empresa, el bot lo dice explícitamente en vez de
   devolver sus propias cifras como si nada.
3. Si aun así llegara una consulta a la base de datos, el RLS la filtra.

Un detalle deliberado: intentar corregir un viaje de otra empresa devuelve el
mismo `404` que un viaje inexistente. Un `403` confirmaría que ese viaje existe,
que ya es información sobre el cliente ajeno.

### Requisito 4 — Métricas globales solo para roles autorizados

**Decisión:** tres roles con **permisos explícitos**, no una jerarquía:

| Rol | consultar | corregir | ver_global |
|---|:-:|:-:|:-:|
| usuario | ✔ | | |
| operador | ✔ | ✔ | |
| auditor | ✔ | | ✔ |

La primera versión usaba una jerarquía lineal (`usuario < operador < auditor`) y
eso daba al auditor el permiso de escritura del operador: podía corregir viajes
de cualquier empresa. Quien audita no debe poder alterar lo auditado. El
permiso exigido se comprueba en tres sitios: al construir el catálogo de
herramientas, en la dependencia de FastAPI y en la propia política RLS, que
concede visión global cuando `app.rol` es `auditor`.

### Requisito 5 — Evitar que una empresa consuma todos los recursos

**Decisión:** limitador de ventana deslizante en Redis, con la cuota
**por empresa** (no por usuario) configurada en la tabla `empresas`.

Que sea por empresa es lo que cumple el requisito: si fuera por usuario, una
empresa con cien cuentas podría agotar la plataforma sin que ninguno de sus
usuarios pasara su límite individual. Las operaciones de escritura cuestan el
doble que las de lectura, porque disparan el recálculo de un cubo.

El consumo se comprueba y se anota en un único script Lua, atómico en Redis.
La versión anterior leía el contador y escribía en dos pasos, y con peticiones
simultáneas (o varias réplicas de la API) se podía superar el límite.

Un detalle de usabilidad que también es de recursos: el panel web pide todo lo
que pinta en **una** petición (`/datos/panel`), que cuesta una unidad. Antes
hacía cinco por refresco y un operador que corregía tres viajes seguidos se
quedaba bloqueado por su propia cuota.

**Dónde:** `api/app/cuotas.py`.

---

## Métricas de calidad de la solución

Tres métricas, elegidas porque miden exactamente lo que las restricciones
exigen. Se consultan en vivo en `GET /metricas/calidad`.

| Métrica | Qué mide | Restricción |
|---|---|---|
| **Aislamiento entre empresas** | Intentos de acceso a datos ajenos bloqueados sobre el total registrado en auditoría. Objetivo: 100 %. | E7 |
| **Latencia de propagación de correcciones** | Milisegundos entre registrar una corrección y tener el cubo de métricas recalculado. | E6 |
| **Cobertura de trazabilidad** | Porcentaje de correcciones que conservan valor original, motivo y autor. Objetivo: 100 %. | E6 |

---

## Cómo se adaptaría a otras restricciones

El enunciado valora explícitamente la *capacidad de adaptar la solución a otras
restricciones*. Partiendo de esta arquitectura:

- **E5, Trazabilidad absoluta** — es casi inmediato: `correcciones` y
  `auditoria_accesos` ya son registros de solo-añadir. Faltaría extender el
  mismo patrón a la ingesta (guardar el fichero de origen y su suma de
  verificación) y encadenar los registros con un hash del anterior para impedir
  manipulaciones.
- **E3, Privacidad total** — el punto de corte sería `repositorio.py`: aplicar
  agregación mínima (rechazar respuestas con menos de *k* viajes) y enmascarar
  identificadores antes de devolver nada al chatbot.
- **E8, Retención y ciclo de vida** — añadir una política de particionado por
  fecha en `viajes` y un proceso de archivado que consolide los cubos antiguos
  y descarte el detalle, conservando las métricas agregadas.
- **E1, Evolución de esquemas** — la ingesta ya normaliza el CSV a un esquema
  propio, que es el punto donde habría que introducir versionado de esquema y
  una tabla de compatibilidad.
