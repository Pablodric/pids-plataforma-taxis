# ADR 0004 · Permisos explícitos por rol; el auditor no escribe

**Contexto.** La v2 usaba una jerarquía lineal `usuario < operador < auditor`.
Al ejecutarla se vio que el auditor heredaba el permiso de escritura y podía
**corregir viajes de cualquier empresa**.

**Decisión.** Una tabla de permisos (`consultar`, `corregir`, `ver_global`)
por rol, con el auditor en solo lectura. Se aplica en tres sitios independientes:
- el catálogo de herramientas del chatbot;
- la dependencia de FastAPI;
- la base de datos: política RLS de inserción con `app.rol = 'operador'` y FK
  compuesta `(viaje_id, empresa_id)`.

**Consecuencias.**
- (+) Quien audita no puede alterar lo auditado (segregación de funciones).
- (+) Las pruebas atacan la base de datos directamente con SQL y confirman que
  la tercera barrera aguanta aunque fallen las dos primeras.
- (−) Un rol que necesite leer y escribir en todo tendría que declararse
  explícitamente. Es intencionado.
