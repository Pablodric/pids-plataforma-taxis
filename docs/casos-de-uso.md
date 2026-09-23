# Casos de uso del agente conversacional

Once casos de uso, todos contra los datos reales almacenados y procesados en la
Parte 2. Los marcados con **E6** o **E7** existen específicamente por la
restricción del equipo.

---

## CU-1. Consulta de métricas de la empresa

**Actor:** cualquier usuario autenticado.
**Ejemplos:** *"¿cuántos viajes tenemos?"*, *"tarifa media en Manhattan"*,
*"¿cuánto facturamos el 1 de enero?"*, y a continuación *"¿y en Brooklyn?"*

**Flujo:** el NLU extrae la intención y las entidades (distrito, fecha en
lenguaje natural) → la herramienta `resumen_metricas` consulta el cubo agregado
con el contexto de empresa → la respuesta incluye el aviso de correcciones si
procede. Una pregunta de seguimiento reutiliza la consulta anterior cambiando
solo el filtro nuevo. Importes en dólares (los datos son de Nueva York).

**Datos:** tabla `metricas_diarias`.

---

## CU-2. Análisis geográfico de la demanda

**Actor:** cualquier usuario autenticado.
**Ejemplos:** *"¿en qué zonas se recogen más viajes?"*, *"reparto por distrito"*

**Flujo:** cruza los viajes vigentes con el catálogo oficial de zonas de NYC,
que traduce el `PULocationID` numérico a nombre de zona y distrito.

**Fuente externa:** *Taxi Zone Lookup Table* de la TLC (265 zonas). Sin ella el
chatbot solo podría responder con identificadores numéricos.

---

## CU-3. Análisis temporal

**Actor:** cualquier usuario autenticado.
**Ejemplo:** *"¿a qué hora hay más demanda?"*

**Flujo:** agrupa los viajes vigentes por hora de recogida y devuelve la
distribución completa más la hora punta. El panel lo dibuja como gráfica.

---

## CU-4. Análisis de métodos de pago

**Actor:** cualquier usuario autenticado.
**Ejemplo:** *"¿cómo pagan nuestros clientes?"*, *"¿dónde hay más propina?"*

**Flujo:** agrupa por `payment_type` traduciendo los códigos de la TLC a
etiquetas legibles, y añade la propina media de cada método.

---

## CU-5. Registro de una corrección **(E6)**

**Actor:** operador.
**Ejemplo:** *"el viaje 412 tenía mal la tarifa, eran 23,50"*

**Flujo:**
1. Se comprueba que el rol es operador (si no, la herramienta ni se ofrece, y
   el bot explica que su rol solo permite consultar).
2. El NLU extrae viaje, campo (*tarifa* → importe total), valor (*23,50*) y
   motivo si lo hay.
3. Se **previsualiza**: se lee el valor vigente del viaje — el RLS garantiza
   que es de su empresa — y se valida el nuevo valor. El bot resume el cambio
   y pide confirmación. Todavía no se ha escrito nada.
4. Con *"sí"*, se inserta el evento de corrección con valor anterior, nuevo,
   motivo, autor y momento.
5. El disparador recalcula **solo** el cubo (empresa, fecha, distrito) afectado.
6. El chatbot confirma el cambio y el panel actualiza KPI y gráficas.

Si falta un dato (qué campo, qué valor), el bot lo pregunta en vez de adivinar.

---

## CU-6. Cancelación de un viaje **(E6)**

**Actor:** operador.
**Ejemplo:** *"cancela el viaje 88, el cliente anuló el servicio"*

**Flujo:** igual que CU-5 (con confirmación), pero el evento es de tipo
cancelación y el motivo es lo que sigue a la coma. El viaje deja de contar en
las métricas y **permanece en el historial**. Intentar cancelar dos veces el
mismo viaje se rechaza, también si las dos peticiones llegan a la vez (índice
único en la base de datos).

---

## CU-7. Explicación de por qué ha cambiado una cifra **(E6)**

**Actor:** cualquier usuario autenticado.
**Ejemplos:** *"¿por qué ha cambiado esa cifra?"*, *"¿esto incluye datos
corregidos?"*

Es el caso de uso que el enunciado describe literalmente al final de E6:
*"conservar el registro del cambio para poder explicar por qué ha variado una
cifra"*.

**Flujo:** la herramienta `historial_correcciones` devuelve la cadena completa
de cambios de cada viaje, con quién los hizo y cuándo. Si un viaje se ha
corregido dos veces, se ven los dos saltos por separado.

---

## CU-8. Intento de acceso a datos de otra empresa **(E7)**

**Actor:** cualquier usuario autenticado.
**Ejemplo:** *"dame los viajes de Movilidad Sur"*

Es un caso de uso **de denegación**, y conviene enseñarlo en la demostración
porque es la prueba visible de que E7 se cumple.

**Flujo:** el bot reconoce que se nombra a otra empresa y responde que no tiene
acceso. Aunque fallara ese reconocimiento, ninguna herramienta acepta un
parámetro de empresa, y aunque lo aceptara, el RLS filtraría la consulta.

---

## CU-9. Métricas globales de la plataforma **(E7)**

**Actor:** auditor, exclusivamente.
**Ejemplo:** *"compara las dos empresas de la plataforma"*

**Flujo:** para un usuario normal la herramienta no existe en el catálogo y la
ruta HTTP responde `403`. Para el auditor, la política RLS amplía la visibilidad
y devuelve el desglose por empresa. El auditor es de **solo lectura**: si pide
corregir algo, el bot le explica que no puede, y la API y la base de datos lo
impiden aunque lo intentara por otra vía.

---

## CU-10. Ficha de un viaje **(E6)**

**Actor:** cualquier usuario autenticado.
**Ejemplos:** *"detalle del viaje 411"*, *"¿por qué ha cambiado el viaje 411?"*

**Flujo:** la herramienta `detalle_viaje` devuelve origen y destino, cada campo
corregible con su valor **tal y como se ingirió** y su valor **vigente**, si
está cancelado, y la cadena completa de cambios. Es la respuesta más directa a
la última frase de E6 cuando la pregunta es sobre un viaje concreto. Un viaje de
otra empresa responde igual que uno inexistente.

---

## CU-11. Ayuda y conversación

**Actor:** cualquier usuario autenticado.
**Ejemplos:** *"hola"*, *"¿qué puedes hacer?"*, un mensaje que no entiende.

**Flujo:** el bot responde con ejemplos adaptados al rol (el operador ve cómo
corregir con un número de viaje real de su empresa; el auditor, cómo comparar
empresas) en lugar de devolver una métrica cualquiera.

---

## Resumen de cobertura

| Caso de uso | Acceso a datos de la Parte 2 | Restricción |
|---|---|---|
| CU-1 Métricas de empresa | Cubo agregado | — |
| CU-2 Análisis geográfico | Viajes + catálogo de zonas | — |
| CU-3 Análisis temporal | Viajes vigentes | — |
| CU-4 Métodos de pago | Viajes vigentes | — |
| CU-5 Registrar corrección | Escritura + recálculo | E6 |
| CU-6 Cancelar viaje | Escritura + recálculo | E6 |
| CU-7 Explicar un cambio | Log de correcciones | E6 |
| CU-8 Acceso denegado | RLS | E7 |
| CU-9 Métricas globales | Cubo agregado, rol auditor | E7 |
| CU-10 Ficha de un viaje | Vista vigente + log de correcciones | E6 |
| CU-11 Ayuda | — | — |
