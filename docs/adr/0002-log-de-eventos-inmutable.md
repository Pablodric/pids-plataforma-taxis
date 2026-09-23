# ADR 0002 · Las correcciones son eventos; los hechos no se modifican

**Contexto.** E6: los proveedores corrigen o cancelan viajes ya procesados, y
hay que conservar el valor original, la corrección y el momento.

**Decisión.** `viajes` es inmutable. Cada corrección o cancelación es una fila
nueva en `correcciones`, y el estado actual se deriva con la vista
`v_viajes_vigentes`. Disparadores impiden `UPDATE` y `DELETE` en ambas tablas
(incluso al superusuario, salvo que los desactive: ver ADR 0006).

**Alternativas descartadas.**
- *`UPDATE` del viaje*: destruye el valor anterior.
- *Versionar filas (v1, v2…)*: duplica el hecho entero por un campo y llena las
  consultas de `MAX(version)`.

**Consecuencias.**
- (+) El pasado se puede reconstruir en cualquier instante sin guardar copias:
  `viajes_en(instante)` aplica solo las correcciones hechas hasta entonces
  (endpoint `/datos/en`, «viaje en el tiempo» del panel).
- (+) Cada salto de una cifra tiene autor, motivo y momento.
- (−) Leer el estado vigente cuesta más que leer una fila. Se compensa con el
  cubo del ADR 0003 y con un `LATERAL` indexado por viaje.
