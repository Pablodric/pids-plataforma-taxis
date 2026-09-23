# Cambios de la versión 5: auditoría, tiempo y operación

| Qué | Para qué |
|---|---|
| **Viaje en el tiempo**: `viajes_en(instante)`, `GET /datos/en`, botón «⏱ ver cifras de antes», pregunta en el chat | Reconstruir las métricas de cualquier instante pasado desde el log de eventos, sin snapshots |
| **Cadena SHA-256 por empresa** en `correcciones` + `GET /auditoria/cadena` + sello en el panel | Detectar la manipulación del historial, incluso por un superusuario. Se verifica con una implementación independiente en Python |
| **Invariante del cubo**: `consistencia_cubo()` en `/metricas/calidad` | Demostrar que el recálculo incremental coincide con un recálculo completo |
| **`/metrics` Prometheus**, `X-Request-ID` (nginx → API) y logs JSON | Operar la plataforma: latencias por ruta, 429 por empresa, motor NLU usado |
| **Bloqueo del login** tras 5 fallos por correo (o 30 por IP) | Frenar la fuerza bruta contra las contraseñas |
| **`limit_req` en nginx** por token | Cortar ráfagas antes de que lleguen a Python |
| **Prueba de carga** `scripts/vecino_ruidoso.py` | Medir si una empresa nota el abuso de otra. **Destapó un fallo propio**: cada 429 escribía en Postgres. Corregido: el p95 de la otra empresa bajo ataque pasó de 187 a 66 ms, con 0 errores |
| **CI**: `ruff`, cobertura ≥ 80 % (85 % actual), prueba de manipulación como superusuario | Calidad verificable en cada push |
| **7 ADR** en `docs/adr/` | Decisiones con alternativas y consecuencias, incluidas las negativas |
| **Diagrama de arquitectura** (`docs/arquitectura.png`) generado desde código | Que la documentación no se desfase |

Pruebas: de 84 a 100. El volumen de la base de datos pasa a `datos_db_v4` porque el esquema cambia.

---

# Cambios de la versión 4: modelo local afinado (Ollama + LoRA)

El chatbot entiende las frases con un modelo de lenguaje **local**: Qwen2.5
afinado con LoRA para esta plataforma y servido por Ollama. Sustituye a la
API de pago como opción por defecto. Detalle completo en [`llm/README.md`](llm/README.md).

| Pieza | Qué es |
|---|---|
| `api/app/esquema_nlu.py` | Contrato JSON del NLU (18 intenciones, 6 entidades) y prompt; lo comparten la API, el dataset, el entrenamiento y la evaluación |
| `api/app/nlu_llm.py` | Cliente de Ollama: esquema forzado en `format`, validación y **anclaje** de entidades al texto (descarta viajes inventados) |
| `api/app/motor.py` | El NLU es enchufable: modelo afinado → modelo base → reglas. Permisos, confirmaciones y cifras no dependen del modelo |
| `llm/generar_dataset.py` | 2.639 / 278 / 556 ejemplos; el test usa plantillas no vistas |
| `llm/entrenar.py` + `entrenar_colab.ipynb` | LoRA r=16 con la pérdida solo sobre la respuesta; GPU gratuita de Colab |
| `llm/exportar.py` | GGUF (llama.cpp) + `Modelfile` con plantilla ChatML |
| `llm/evaluar.py` | Reglas vs modelo base vs modelo afinado: intención, F1, entidades, marco exacto, latencia |
| `docker-compose.yml` | Servicios `ollama` y `ollama-init`: instala `pids-nlu` o, si aún no existe, descarga el modelo base |
| Panel | La cabecera dice qué modelo responde (afinado, base o reglas) |
| Pruebas | +17 (84 en total) contra un servidor que habla el protocolo de Ollama |

Línea base medida sobre el test de frases no vistas. NLU por reglas: 65,3 %
de intención y 62,6 % de marco exacto. El cuaderno de Colab mide el modelo
base y el afinado sobre el mismo test.

---

# Cambios de la versión 3

La versión 2 arrancaba y pasaba sus 19 pruebas, pero al ejecutarla de verdad
aparecieron fallos que esas pruebas no cubrían. Este documento los lista por
gravedad, con lo que se hizo en cada caso. Todos tienen ahora una prueba de
regresión en `tests/test_mejoras.py`.

## Fallos corregidos

### 1. El auditor podía modificar datos de cualquier empresa (E7, seguridad)

**Síntoma:** `POST /correcciones` con el token del auditor sobre un viaje de
Taxis del Norte devolvía `200` y registraba la corrección.

**Causa:** los roles eran una jerarquía lineal (`usuario < operador < auditor`),
así que el auditor heredaba el permiso de escritura del operador. La política
RLS de inserción solo comprobaba que la empresa de la corrección fuese la de la
sesión, y el auditor podía anotar una corrección *de su empresa* sobre el viaje
*de otra*, porque la FK solo apuntaba a `viaje_id`.

**Solución, en tres capas independientes:**
- `auth.py`: permisos explícitos por rol (`consultar`, `corregir`,
  `ver_global`). El auditor lee todo y no escribe nada.
- Base de datos: FK compuesta `(viaje_id, empresa_id) → viajes(id, empresa_id)`.
  Una corrección solo puede pertenecer a la empresa dueña del viaje, se llame
  desde donde se llame.
- Política RLS de inserción: exige además `app.rol = 'operador'`.

### 2. El panel dejaba bloqueado al operador con `429` (E7, usabilidad)

**Síntoma:** tras tres correcciones seguidas desde el panel, todo respondía
`429 Too Many Requests` durante un minuto.

**Causa:** cada refresco del panel hacía 5 peticiones y cada una consumía
cuota (5 + 2 por corrección, con un límite de 20 por minuto).

**Solución:** nuevo endpoint `GET /datos/panel` que devuelve todo en una
transacción y una sola unidad de cuota. La cabecera del panel muestra además la
cuota restante (cabeceras `X-Cuota-Limite` y `X-Cuota-Restante`).

### 3. El chatbot sin clave no cubría la mitad de los casos de uso

**Síntoma:** en modo reglas (el modo por defecto),
- *«el viaje 411 tenía mal la tarifa, eran 23,50»* o *«cancela el viaje 88»*
  devolvían el resumen de métricas: CU-5 y CU-6 solo funcionaban con LLM;
- *«¿cuánto facturamos el 1 de enero?»* ignoraba la fecha;
- *«hola»* o cualquier cosa no entendida devolvía métricas;
- *«¿cuántos viajes hay ahora?»* se interpretaba como pregunta por horas
  («ahora» contiene «hora»);
- *«compara las dos empresas»* no se reconocía.

**Solución:** NLU reescrito en su propio módulo (`nlu.py`) con coincidencia por
palabra completa, extracción de viaje, campo, valor (con coma decimal) y motivo,
fechas en lenguaje natural resueltas contra las fechas que hay en los datos, y
preguntas de seguimiento (*«¿y en Brooklyn?»*). Las escrituras se previsualizan
y **piden confirmación** antes de registrarse. Saludo, ayuda contextual por rol
y respuesta honesta cuando no entiende.

### 4. Cualquier frase con «plataforma» se denegaba

**Síntoma:** *«Dame las métricas globales de la plataforma»* (una de las
sugerencias del propio panel) respondía «No tengo acceso a los datos de
Operador de la plataforma» a cualquier usuario, incluido el auditor.

**Causa:** el detector de «otra empresa» comparaba contra todas las empresas,
incluida la cuenta técnica `plataforma`, y sin límites de palabra.

**Solución:** solo cuentan las empresas con datos propios, con límites de
palabra y variantes del nombre (*«Taxis del Norte»*, *«taxis norte»*). El
auditor, que sí puede ver otras empresas, recibe la comparativa global.

### 5. En modo LLM, el segundo mensaje de cada conversación fallaba

**Síntoma:** el primer mensaje funcionaba; a partir del segundo, la API del LLM
rechazaba la conversación (error 400) y el chat caía siempre a modo reglas.

**Causa:** el historial guardaba en Redis los bloques del SDK convertidos con
`str()`. Al reenviarlos, la API los rechazaba. Además, recortar el historial
podía dejar un `tool_use` sin su `tool_result`.

**Solución:** el historial solo guarda turnos de texto. Además, si el modelo
falla *después* de haber registrado una corrección, ya no se repite el mensaje
en modo reglas (habría duplicado la corrección): se informa de lo hecho.

### 6. Otros fallos

| Fallo | Solución |
|---|---|
| Importes mostrados en **EUR**; los datos son de Nueva York, en **dólares** | USD en API, chat y panel |
| Postgres y Redis publicaban los puertos 5432 y 6379: podían chocar con un Postgres o Redis instalado en el equipo | Ya no se publican (no hacen falta) |
| Chart.js se cargaba de un CDN: sin internet el panel no arrancaba | Incluido en `web/vendor/`; el panel funciona aunque falte |
| El panel llamaba a `http://host:8000` a mano | nginx hace de proxy en `/api` (mismo origen, sin CORS) |
| El pool de conexiones no se podía reabrir (`PoolClosed` en recargas y pruebas) | Pool nuevo en cada arranque, con comprobación de conexiones rotas |
| Cuota no atómica: con peticiones simultáneas se podía superar el límite | Script Lua en Redis (comprobar y consumir en un solo paso) |
| Dos cancelaciones simultáneas del mismo viaje podían pasar las dos | Índice único parcial en la base de datos (`409` a la segunda) |
| Dos correcciones simultáneas del mismo viaje podían romper la cadena v0 → v1 → v2 | Bloqueo consultivo por viaje |
| Se podían registrar valores absurdos (150000 $, 2,5 pasajeros, distancia negativa) | Validación por campo (`422`) |
| Todos los errores de corrección devolvían `404` | `404` no existe · `409` conflicto (cancelado, ya cancelado) · `422` valor no válido · `403` permiso |
| Campo de valor `type=number`: según el idioma del navegador, «7,5» se rechazaba | Campo de texto que acepta coma o punto |
| `JWT_SECRETO` por defecto de 21 bytes (inseguro para HS256) | Si falta o es corto, se genera uno aleatorio al arrancar |
| Recargar la página cerraba la sesión | La sesión se conserva en `sessionStorage` |
| Tras actualizar el proyecto, el volumen antiguo conservaba el esquema viejo | Volumen renombrado (`datos_db_v3`): base de datos nueva automáticamente |
| La API corría como root en su contenedor | Usuario sin privilegios |

## Mejoras

- **Historial de solo-añadir garantizado por el motor:** disparadores que
  impiden `UPDATE` y `DELETE` sobre `viajes` y `correcciones`, incluso al
  superusuario.
- **Vista vigente más eficiente:** un único `LATERAL` por viaje sobre el índice
  `(viaje_id, aplicada_en)`, en lugar de recorrer toda la tabla de correcciones.
- **Ficha de viaje** (`GET /datos/viajes/{id}` y herramienta `detalle_viaje`):
  valores originales, vigentes y cadena completa de cambios.
- **Ingesta trazable:** tabla `ingestas` con fichero, huella SHA-256 y recuento
  de filas cargadas, descartadas y anómalas (fechas fuera de 2020, importes
  negativos, distancias cero). Visible en `/metricas/calidad`.
- **Denegaciones de permiso auditadas**, para que la métrica de aislamiento las
  cuente.
- **Panel:** muestra el valor vigente antes de corregir, confirma las
  cancelaciones, resalta los KPI que cambian, botones Sí/No para las
  confirmaciones del chat, vista específica del auditor, sugerencias según el
  rol con identificadores de viaje reales, y diseño adaptado a móvil.
- **Pruebas:** de 19 a 67, incluidas pruebas que atacan la base de datos
  directamente saltándose la API.
