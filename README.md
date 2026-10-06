# Plataforma de datos de taxis — E6 + E7

Prueba de concepto de una plataforma de datos de viajes en taxi con agente
conversacional, desarrollada para las restricciones:

- **E6. Datos corregibles** — los proveedores pueden corregir o cancelar viajes
  ya ingeridos; las métricas se actualizan sin perder el historial.
- **E7. Plataforma multiempresa** — varias empresas comparten infraestructura
  pero cada una solo ve sus propios datos.

Los datos son **reales**: 999 viajes del dataset público *2020 Yellow Taxi Trip
Data* de la ciudad de Nueva York (importes en **dólares**), enriquecidos con el
catálogo oficial de las 265 zonas de la TLC.

Además del histórico, los viajes nuevos entran **en tiempo real** por la API:
un simulador hace de proveedor de cada empresa y el panel se actualiza solo.

El asistente entiende las frases con un **modelo de lenguaje local** (Qwen2.5
de 1.500 M de parámetros, servido por Ollama) y, si el modelo falla o tarda,
con reglas. Sin API de pago y sin que los datos salgan de la máquina. Afinar el
modelo con LoRA está preparado pero **no se ha ejecutado** (faltó GPU): el
dataset, el cuaderno y la evaluación están en [`llm/README.md`](llm/README.md).

![Arquitectura](docs/arquitectura.png)

### Lo que la distingue

- **Ingesta en tiempo real.** Cada empresa envía sus viajes por
  `POST /ingesta/viajes` con una cuenta de proveedor; la empresa del viaje sale
  de esa credencial, nunca del dato. Un disparador recalcula solo el cubo
  afectado y el panel se entera por WebSocket, sin recargar.
- **Viaje en el tiempo.** Las cifras de cualquier instante pasado se
  reconstruyen desde el log de eventos, sin snapshots. Pulsando «⏱ ver cifras
  de antes» en el historial, o preguntando *«¿cuánto facturábamos antes de la
  última corrección?»*.
- **Historial a prueba de manipulaciones.** Cadena SHA-256 por empresa: si
  alguien altera una corrección, incluso como superusuario de la base de datos,
  el panel lo marca en rojo.
- **Aislamiento en tres barreras** (API, FK compuesta, Row Level Security),
  probado atacando la base de datos directamente con SQL.
- **Invariante verificado.** El cubo de métricas incremental se compara con un
  recálculo completo desde el log (`/metricas/calidad`).
- **Medido, no supuesto.** Hay una prueba de carga «vecino ruidoso» con números
  reales, métricas Prometheus, `X-Request-ID` de extremo a extremo y CI con
  lint, 129 pruebas y cobertura mínima del 80 %.
- **Decisiones documentadas** como ADR ([`docs/adr/`](docs/adr/)), incluidas
  las consecuencias negativas.

> Qué ha cambiado respecto a versiones anteriores y por qué: [`CAMBIOS.md`](CAMBIOS.md).

---

## 1. Puesta en marcha

Requisitos: Docker Desktop instalado y arrancado (que ponga *Engine running*).

```bash
docker compose up --build
```

Y ya está. El fichero `.env` es **opcional**. La primera vez el arranque crea la
base de datos, aplica el esquema, carga el CSV, calcula las métricas iniciales,
arranca el simulador (un viaje nuevo cada 3 s) y descarga en Ollama el modelo
`qwen2.5:1.5b` (~1 GB, solo la primera vez).

La plataforma **no espera** a esa descarga: mientras tanto el chat responde con
las reglas, y la cabecera del panel indica en cada momento qué motor responde.

**Equipos con poca memoria.** La imagen de Ollama pesa casi 4 GB. Para arrancar
todo lo demás sin ella, con el chat por reglas:

```bash
docker compose -f docker-compose.ligero.yml up --build
```

Para ver los viajes que envía el simulador: `docker compose logs -f simulador`.
Para pararlo sin parar el resto: `docker compose stop simulador`.

Cuando termine, abre en el navegador:

| Dirección | Qué es |
|---|---|
| http://localhost:8080 | **Panel web**: chat, gráficas y correcciones |
| http://localhost:8000/docs | Documentación interactiva de la API |
| http://localhost:8000/salud | Estado de los componentes |
| http://localhost:8000/metricas/calidad | Métricas de calidad y calidad de la ingesta |

El panel habla con la API a través de nginx (`/api`, mismo origen), así que
también funciona desde otro equipo de la red y **sin conexión a internet**
(Chart.js va incluido en `web/vendor/`).

### Usuarios de prueba

Contraseña para todos: `demo1234`. En el panel basta con pulsar el usuario.

| Correo | Empresa | Rol | Qué puede hacer |
|---|---|---|---|
| `ana@taxisnorte.es` | Taxis del Norte | usuario | Consultar sus métricas |
| `luis@taxisnorte.es` | Taxis del Norte | operador | Además, corregir y cancelar viajes |
| `marta@movilidadsur.es` | Movilidad Sur | usuario | Consultar sus métricas |
| `pablo@movilidadsur.es` | Movilidad Sur | operador | Además, corregir y cancelar viajes |
| `auditor@plataforma.es` | Plataforma | auditor | Ver todas las empresas, **solo lectura** |
| `sistema@taxisnorte.es` | Taxis del Norte | proveedor | Solo enviar viajes nuevos (cuenta de máquina) |
| `sistema@movilidadsur.es` | Movilidad Sur | proveedor | Solo enviar viajes nuevos (cuenta de máquina) |

Las dos cuentas de proveedor son las que usa el simulador. No aparecen en la
pantalla de entrada porque no pueden consultar ni usar el chat.

### Motores del chatbot

| `LLM_PROVEEDOR` | Qué entiende las frases | Cuándo usarlo |
|---|---|---|
| `ollama` (por defecto) | Modelo local `qwen2.5:1.5b`, con unos pocos ejemplos en el mensaje | Lo normal |
| `reglas` | NLU por reglas en español (`api/app/nlu.py`) | Equipos muy justos de memoria |
| `anthropic` | Agente con *function calling* sobre la API de Anthropic (`LLM_API_KEY`) | Si tienes clave |

En los tres, las cifras salen de la base de datos, cada corrección pide
confirmación y el aislamiento entre empresas no depende del modelo. Si el
modelo falla (no responde, tarda o devuelve algo inválido), ese mensaje lo
atienden las reglas.

Para cambiar de motor: `LLM_PROVEEDOR=reglas docker compose up`, o ponlo en `.env`.

### Más datos: viajes sintéticos

La muestra real son 999 viajes de unas pocas horas del 1 de enero. Para
probar con un año entero y más volumen:

```bash
python scripts/generar_datos_sinteticos.py 10000      # crea ingest/datos/sinteticos.csv
docker compose down -v                                 # la ingesta solo carga una base vacía
DATOS_VIAJES=sinteticos.csv docker compose up -d
```

Cada viaje sintético parte de uno real y cambia el día (dentro de 2020), la
distancia, la tarifa, la propina y las zonas. Conserva la empresa y la
proporción de anomalías, así que E6 y E7 se prueban igual. La misma semilla
(`--semilla`, 42 por defecto) genera siempre el mismo fichero, que por eso no
se sube a Git. Para volver a los datos reales: `docker compose down -v` y
`docker compose up -d`.

---

## 2. Guion de demostración

Cinco escenas que enseñan las dos restricciones. Sirven tal cual para el vídeo
o las capturas de la entrega.

### Escena 1 — Aislamiento entre empresas (E7)

1. Entra como **ana@taxisnorte.es** y pregunta *«¿cuántos viajes tenemos?»* →
   400 viajes.
2. Pulsa la sugerencia *«Dame los datos de Movilidad Sur»* → el bot responde
   que no tiene acceso a esa empresa.
3. Sal y entra como **marta@movilidadsur.es**, misma pregunta → 599 viajes.
   Las gráficas del panel también cambian: son otros datos.

### Escena 2 — Una corrección se propaga (E6)

1. Entra como **luis@taxisnorte.es** (operador) y anota la facturación total.
2. En *Registrar corrección*: elige un viaje, campo **Importe total**, pon un
   valor claramente distinto (por ejemplo `150`) y un motivo. El formulario
   muestra el valor vigente antes de cambiarlo.
3. Pulsa **Aplicar corrección**: los KPI se actualizan (parpadean en verde los
   que cambian) y el indicador *corregidos / cancelados* sube.
4. Pregunta al chat *«¿por qué ha cambiado esa cifra?»* → devuelve valor
   anterior, valor nuevo, motivo, autor y momento exacto.

### Escena 3 — Corregir hablando con el asistente (E6)

1. Con el mismo usuario escribe *«el viaje 411 tenía mal la tarifa, eran
   23,50»* (o pulsa la sugerencia *«Corrige el viaje …»*).
2. El asistente resume el cambio (valor actual → nuevo, motivo) y pide
   confirmación con botones **Sí / No**. No escribe nada hasta confirmar.
3. Pulsa **Sí** → se registra, y el panel se actualiza solo.
4. *«Detalle del viaje 411»* → ficha con valores originales, vigentes y la
   cadena completa de cambios.

### Escena 4 — Cancelación sin perder historial (E6)

1. *«Cancela el viaje 411, el cliente anuló el servicio»* → confirma.
   (O desde el formulario, botón **Cancelar viaje**.)
2. El número de viajes baja en uno, pero el viaje sigue en el historial de
   correcciones. No se ha borrado nada, y no se puede cancelar dos veces.

### Escena 5 — Roles y cuota (E7)

1. Como **ana** (usuario), pide *«dame las métricas globales»* → denegado.
2. Entra como **auditor@plataforma.es** → el panel muestra la comparativa entre
   empresas, pero **no** el formulario de correcciones: el auditor lo ve todo
   y no puede modificar nada (ni por el panel, ni por el chat, ni por la API).
3. El contador *cuota* de la cabecera muestra el consumo de la empresa. Para
   enseñar el límite, lanza muchas consultas seguidas desde una empresa: a
   partir de la cuota responde `429` y la otra empresa sigue funcionando.
   Con números: `python scripts/vecino_ruidoso.py --url http://localhost:8080/api`
   (resultados en el [ADR 0007](docs/adr/0007-cuota-en-dos-capas.md)).

### Escena 6 — Viaje en el tiempo y manipulación detectada (E6)

1. Como **luis**, haz dos correcciones y pulsa **⏱ ver cifras de antes** en el
   historial: el panel reconstruye las métricas de ese instante (entonces /
   ahora / diferencia) sin copias guardadas.
2. Fíjate en el sello **🔒 Cadena íntegra** del historial. Ahora simula a un
   administrador deshonesto que edita una corrección a mano:
   ```bash
   docker compose exec db psql -U postgres -d pids -c "ALTER TABLE correcciones DISABLE TRIGGER USER; UPDATE correcciones SET valor_nuevo = 1 WHERE id = (SELECT min(id) FROM correcciones); ALTER TABLE correcciones ENABLE TRIGGER USER;"
   ```
3. Recarga el panel: el sello pasa a **⚠ Cadena rota**, con el eslabón y el
   motivo («su contenido no coincide con su hash»). `GET /auditoria/cadena`
   da el detalle.

### Flujo del chatbot

Cómo se procesa cada mensaje: autenticación, cuota, NLU con respaldo, confirmaciones,
viaje en el tiempo y permisos.
[`docs/flujo_chatbot.png`](docs/flujo_chatbot.png)

### Escena 7 — Viajes en tiempo real (E7)

1. Abre dos ventanas: el panel como **ana** y una terminal con
   `docker compose logs -f simulador`.
2. Cada línea `viaje N -> taxis_norte` de la terminal sube en uno el contador
   de viajes de Ana, sin recargar (etiqueta verde «en directo»).
3. Las líneas `-> movilidad_sur` no mueven su panel: son de la otra empresa.
   Entra como **marta** y verás lo contrario.
4. La empresa del viaje sale de la cuenta con la que se envía, no del dato:
   aunque un proveedor cambiara el `VendorID`, el viaje se guardaría como suyo.

### Observabilidad

- `http://localhost:8000/metrics`: formato Prometheus, con latencias por ruta
  (histograma), rechazos 429 por empresa, correcciones, mensajes por motor
  NLU e intentos de login bloqueados.
- Cada respuesta lleva `X-Request-ID`, el mismo que nginx y el que aparece en
  los logs JSON de la API (`docker compose logs api`).

---

## 3. Comprobar que funciona de verdad

Hay **129 pruebas automáticas** (cobertura del 86 %, `ruff` sin avisos) que se ejecutan contra la API, el Postgres y el
Redis reales, no contra simulaciones. La única excepción es el modelo de
lenguaje: se sustituye por un servidor que habla el protocolo de Ollama, para
probar la integración sin descargar un modelo. La calidad del modelo se mide
aparte, con `llm/evaluar.py`.

```bash
docker compose exec -e PYTHONPATH=/srv api python -m pytest /srv/tests -v -p no:cacheprovider
```

Las pruebas cubren, entre otras cosas:

- Que las dos empresas ven conjuntos de viajes **disjuntos**, y que sus totales
  suman exactamente lo ingerido.
- Que un operador **no puede corregir un viaje de otra empresa**, ni por la API
  (mismo 404 que un viaje inexistente) ni saltándose la API con SQL directo
  (lo impide la FK compuesta en la base de datos).
- Que el **auditor no puede escribir**, tampoco por SQL (política RLS).
- Que el historial es de **solo-añadir**: no se puede editar ni borrar.
- Que una corrección **conserva el valor anterior** con motivo, autor y momento,
  y que dos correcciones seguidas dejan la cadena v0 → v1 → v2.
- Que una cancelación **saca el viaje de las métricas pero no del historial**, y
  que no se puede cancelar dos veces.
- Que la **cuota por empresa** se aplica, es atómica con 40 peticiones
  simultáneas y que el panel solo gasta una unidad por refresco.
- Que el chatbot **pide confirmación** antes de escribir, entiende fechas y
  preguntas de seguimiento, y que un fallo del modelo no duplica una corrección.
- Que un viaje nuevo entra a nombre de la empresa de la **credencial** aunque
  el dato diga otra, que solo la cuenta de proveedor puede enviarlos (tampoco
  por SQL directo), que la ingesta tiene su propia cuota y que el aviso en
  directo llega **solo a la empresa del viaje**.
- Que con Ollama se envía el esquema JSON y el prompt del contrato,
  que un viaje **inventado por el modelo se descarta**, y que si Ollama no
  responde o devuelve algo inválido el chat sigue funcionando con reglas.
- Que las cifras del pasado se reconstruyen bien (antes y después de una
  corrección o cancelación) y que el pasado también está aislado por empresa.
- Que la cadena de hashes detecta borrados y modificaciones, incluida una
  manipulación **real como superusuario**. Se ejecuta en CI y si defines
  `DATABASE_URL_ADMIN`.
- Que el cubo incremental coincide con un recálculo completo, que `/metrics` no
  usa ids en las etiquetas y que el login se bloquea tras 5 fallos.

---

## 4. Estructura del proyecto

```
.
├── docker-compose.yml       Orquestación de los 8 servicios (con ollama, ollama-init y simulador)
├── docker-compose.ligero.yml  Lo mismo sin Ollama: chat por reglas, para equipos justos
├── docker-compose.gpu.yml   Opcional: Ollama con GPU NVIDIA
├── .env.example             Plantilla de configuración (opcional)
├── CAMBIOS.md               Qué se corrigió en la v3 y por qué
├── db/init/01_esquema.sql   Esquema, vista vigente, RLS y recálculo incremental
├── ingest/                  Carga inicial del CSV y del catálogo de zonas
│   ├── ingesta.py           Con huella SHA-256 e informe de anomalías
│   └── datos/
│       ├── rows.csv                 999 viajes (TLC 2020)
│       ├── sinteticos.csv           (generado, no se sube) ver «Más datos»
│       └── taxi_zone_lookup.csv     265 zonas oficiales de NYC
├── api/app/
│   ├── main.py              Rutas HTTP
│   ├── auth.py              JWT y permisos por rol (E7)
│   ├── cuotas.py            Límite de consumo por empresa, atómico en Redis (E7)
│   ├── db.py                Pool de conexiones y contexto RLS
│   ├── repositorio.py       Todo el SQL de negocio
│   ├── herramientas.py      Catálogo de funciones del chatbot, recortado por rol
│   ├── esquema_nlu.py       Contrato JSON del NLU (modelo, dataset y evaluación)
│   ├── nlu_llm.py           NLU con el modelo de Ollama: esquema, validación y anclaje
│   ├── nlu.py               NLU por reglas (respaldo y referencia de evaluación)
│   ├── tiempo_real.py       Avisos de viaje nuevo al panel: WebSocket, tickets y Redis
│   ├── observabilidad.py    Métricas Prometheus, X-Request-ID y logs JSON
│   └── motor.py             Gestión del diálogo: confirmaciones, contexto y motores
├── simulador/               Proveedor simulado: envía un viaje cada 3 s por la API
├── llm/                     Afinado del modelo: preparado, no ejecutado (ver llm/README.md)
│   ├── generar_dataset.py   Dataset sintético con test de plantillas no vistas
│   ├── entrenar.py          LoRA sobre Qwen2.5-Instruct
│   ├── entrenar_colab.ipynb Todo el proceso en una GPU gratuita de Colab
│   ├── exportar.py          GGUF + Modelfile para Ollama
│   ├── evaluar.py           Reglas vs modelo base vs modelo afinado
│   ├── instalar_en_ollama.sh  Lo ejecuta el servicio ollama-init
│   ├── datos/               Dataset generado (train/val/test)
│   └── modelo/              Aquí iría el modelo afinado (vacío: no se ha entrenado)
├── nginx/default.conf       Servidor del panel y proxy /api
├── web/
│   ├── index.html           Panel de chat, gráficas y correcciones
│   └── vendor/chart.umd.js  Chart.js 4.4.1 (MIT), para no depender de internet
├── tests/                   129 pruebas de E6, E7, ingesta, tiempo real, chatbot y auditoría
├── scripts/
│   ├── vecino_ruidoso.py    Prueba de carga: ¿nota una empresa el abuso de otra?
│   └── generar_datos_sinteticos.py  Más viajes, a partir de los reales
├── .github/workflows/       CI: ruff, pruebas con cobertura, build y arranque con Docker
└── docs/
    ├── arquitectura.png     Diagrama (se regenera con docs/diagramas/arquitectura.py)
    ├── arquitectura_resumen.png  El mismo en ocho cajas, para diapositivas
    ├── flujo_chatbot.png    Flujo de una conversación del chatbot, paso a paso
    ├── adr/                 Registro de decisiones de arquitectura (7 ADR)
    ├── arquitectura.md      Componentes y por qué cada tecnología
    ├── decisiones-e6-e7.md  Cómo se cumple cada requisito del enunciado
    └── casos-de-uso.md      Los casos de uso del chatbot
```

---

## 5. Problemas frecuentes

**`error during connect... docker_engine`** — Docker Desktop no está arrancado.
Ábrelo y espera a que ponga *Engine running*.

**`port is already allocated`** — algo en tu equipo ya usa el puerto 8000 u
8080. Cambia el número de la izquierda en `docker-compose.yml` (por ejemplo
`"8081:80"`). Postgres y Redis ya no publican puertos, así que un Postgres
instalado en tu equipo no interfiere.

**El panel dice que no encuentra la API** — la API tarda unos segundos en
arrancar la primera vez (espera a la ingesta). Comprueba
http://localhost:8000/salud y, si no responde, `docker compose logs api`.

**Me echa al login tras reiniciar** — sin `JWT_SECRETO` en el `.env` se genera
un secreto nuevo en cada arranque de la API, y las sesiones anteriores dejan de
valer. Es lo esperado; pon un secreto fijo si te molesta.

**Quiero empezar de cero** — `docker compose down -v` borra también el volumen
de la base de datos, y el siguiente arranque vuelve a ingerir el CSV.

**Venía de la versión anterior** — no hace falta hacer nada: el volumen tiene
un nombre nuevo (`datos_db_v4`) y la base de datos se crea con el esquema
actual. El volumen antiguo se puede borrar con `docker volume rm pids_datos_db`.

**Inspeccionar la base de datos** — `docker compose exec db psql -U postgres -d pids`.

**El chat va lento** — en CPU el modelo tarda unos segundos por mensaje. Ver
las opciones en [`llm/README.md`](llm/README.md#6-problemas-frecuentes)
(modelo de 0,5B, cuantizar, GPU, Ollama nativo en Mac) o usa
`LLM_PROVEEDOR=reglas`.

**¿Qué modelo está respondiendo?** — la cabecera del panel lo dice, y
`http://localhost:8000/salud` también (`ollama.modelo`, `ollama.afinado`).

---

## 6. Fuentes de datos

- **2020 Yellow Taxi Trip Data**, NYC Open Data —
  https://data.cityofnewyork.us/Transportation/2020-Yellow-Taxi-Trip-Data/kxp8-n2sj/about_data
- **Taxi Zone Lookup Table** (265 zonas), NYC TLC —
  https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page

Nota sobre la muestra: son las primeras 999 filas del fichero, casi todas del
arranque del 1 de enero de 2020 (00:00–04:00). Por eso el panel muestra reparto
por distrito y por método de pago en lugar de la curva horaria, que con esta
muestra no es representativa. La ingesta detecta y registra además 3 viajes
con fecha fuera de 2020, 4 con importe negativo y 12 con distancia cero: se
cargan igualmente (son hechos tal y como los envió el proveedor) y E6 permite
corregirlos. Para una muestra de todo el año, ver «Más datos: viajes
sintéticos» en la puesta en marcha.
