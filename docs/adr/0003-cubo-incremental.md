# ADR 0003 · Cubo materializado con recálculo incremental

**Contexto.** E6 pide actualizar las métricas afectadas por una corrección sin
recalcular todo el histórico.

**Decisión.** Tabla `metricas_diarias` con granularidad (empresa, fecha,
distrito). Un disparador `AFTER INSERT` sobre `correcciones` recalcula **solo**
el cubo del viaje afectado.

**Alternativas descartadas.**
- *Calcular al vuelo*: recorre todo el histórico en cada pregunta.
- *`REFRESH MATERIALIZED VIEW`*: siempre lo recalcula todo.

**Consecuencias.**
- (+) Una corrección recalcula 1 cubo de N; la latencia de propagación medida es
  de pocos milisegundos (`/metricas/calidad`).
- (−) Riesgo de que el cubo diverja si el disparador tuviera un fallo. **Se
  mitiga con un invariante comprobable**: `consistencia_cubo()` compara el cubo
  con un recálculo completo desde el log. Se publica en `/metricas/calidad` y lo
  verifica una prueba tras decenas de correcciones.
