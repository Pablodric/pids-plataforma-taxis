# ADR 0006 · Cadena SHA-256 por empresa en el log de correcciones

**Contexto.** Los permisos y los disparadores de solo-añadir (ADR 0002)
protegen el historial frente a la aplicación, pero no frente a quien administra
la base de datos: un superusuario puede desactivar los disparadores y editar
una fila.

**Decisión.** Cada corrección guarda un número de eslabón, el hash del eslabón
anterior de **su empresa** y el SHA-256 de su contenido canónico, que calcula un
disparador `BEFORE INSERT`, serializado por empresa con un *advisory lock*.
`GET /auditoria/cadena` recalcula la cadena **en Python, con una implementación
independiente** de la de SQL, y señala el primer eslabón roto.

**Alternativas descartadas.**
- *Una cadena global*: serializaría las escrituras de todas las empresas y
  obligaría a cada una a ver hashes de las demás.
- *Encadenar por `id`*: con inserciones concurrentes el orden de los id no es el
  orden de confirmación, y la cadena se bifurcaría.

**Consecuencias.**
- (+) Modificar o borrar una corrección, incluso como superusuario, se detecta.
  La prueba `test_una_manipulacion_como_superusuario_se_detecta` lo hace de
  verdad: desactiva los disparadores, altera un valor, comprueba la alarma y
  restaura.
- (−) No impide la manipulación: la **detecta**. Para impedir que se reescriba la
  cadena entera habría que anclar la cabeza fuera del sistema (publicarla
  periódicamente). Es el paso natural hacia E5.
