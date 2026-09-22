# Plataforma de datos de taxis — E6 + E7

Prueba de concepto de una plataforma de datos de viajes en taxi con agente
conversacional, desarrollada para las restricciones:

- **E6. Datos corregibles** — los proveedores pueden corregir o cancelar viajes
  ya ingeridos; las métricas se actualizan sin perder el historial.
- **E7. Plataforma multiempresa** — varias empresas comparten infraestructura
  pero cada una solo ve sus propios datos.

Los datos son **reales**: 999 viajes del dataset público *2020 Yellow Taxi Trip
Data* de la ciudad de Nueva York, enriquecidos con el catálogo oficial de las
265 zonas de la TLC.

---

## 1. Puesta en marcha

Requisitos: Docker Desktop instalado y arrancado (que ponga *Engine running*).

```bash
cp .env.example .env     # en PowerShell:  copy .env.example .env
docker compose up --build
```

Y ya está. No hay más pasos: la primera vez el arranque crea la base de datos,
aplica el esquema, carga el CSV y calcula las métricas iniciales.

Cuando termine, abre en el navegador:

| Dirección | Qué es |
|---|---|
| http://localhost:8080 | **Panel web**: chat, gráficas y correcciones |
| http://localhost:8000/docs | Documentación interactiva de la API |
| http://localhost:8000/salud | Estado de los componentes |
| http://localhost:8000/metricas/calidad | Las tres métricas de calidad |

### Usuarios de prueba

Contraseña para todos: `demo1234`

| Correo | Empresa | Rol | Qué puede hacer |
|---|---|---|---|
| `ana@taxisnorte.es` | Taxis del Norte | usuario | Consultar sus métricas |
| `luis@taxisnorte.es` | Taxis del Norte | operador | Además, registrar correcciones |
| `marta@movilidadsur.es` | Movilidad Sur | usuario | Consultar sus métricas |
| `pablo@movilidadsur.es` | Movilidad Sur | operador | Además, registrar correcciones |
| `auditor@plataforma.es` | Plataforma | auditor | Además, métricas globales |

### El chatbot no depende de ningún proveedor de pago

El motor de diálogo tiene cuatro modos y se elige con una variable del
`.env`. En los cuatro se usan **las mismas herramientas y la misma capa de
datos**, así que E6 y E7 se comportan igual: el aislamiento y las correcciones
viven en la base de datos, no en el modelo.

| Modo | Coste | Clave | Cuándo usarlo |
|---|---|---|---|
| `reglas` | Ninguno | No | Por defecto. NLU propio por palabras clave. Funciona sin internet. |
| `ollama` | Ninguno | No | Modelo abierto en un contenedor propio. Los datos no salen de la plataforma. |
| `openai` | Gratis o de pago | Sí | Cualquier API compatible con OpenAI: Groq y Gemini tienen nivel gratuito. |
| `anthropic` | De pago | Sí | API de Anthropic. |

**Sin tocar nada arranca en modo reglas**, así que el proyecto se puede evaluar
entero — los nueve casos de uso, E6, E7 y las 22 pruebas — sin registrarse en
ningún sitio ni gastar un céntimo.

**Para usar un LLM local y gratuito** (la opción recomendada, porque es
coherente con el resto del despliegue self-hosted):

```bash
docker compose --profile ollama up
```

y en el `.env`:

```
LLM_PROVEEDOR=ollama
OLLAMA_MODELO=qwen2.5:3b
```

La primera vez descarga unos 2 GB; después ya lo tiene en un volumen. En un
portátil sin GPU tarda algunos segundos por respuesta, que para una
demostración es perfectamente aceptable.

**Para usar un proveedor externo con nivel gratuito** (Groq, por ejemplo):

```
LLM_PROVEEDOR=openai
LLM_API_KEY=tu-clave
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODELO=llama-3.3-70b-versatile
```

Si el proveedor elegido falla en caliente — sin crédito, contenedor parado, red
caída — la petición **se resuelve igualmente en modo reglas** y la respuesta
indica desde qué proveedor se degradó. El panel muestra en la cabecera qué
motor está activo en cada momento.

---

## 2. Guion de demostración

Cuatro escenas que enseñan las dos restricciones. Sirven tal cual para el vídeo
o las capturas de la entrega.

### Escena 1 — Aislamiento entre empresas (E7)

1. Entra como **ana@taxisnorte.es** y pregunta *"¿cuántos viajes tenemos?"* →
   400 viajes.
2. Pregunta *"dame los datos de Movilidad Sur"* → el bot responde que no tiene
   acceso a esa empresa.
3. Sal y entra como **marta@movilidadsur.es**, misma pregunta → 599 viajes.
   Las gráficas del panel también cambian: son otros datos.

### Escena 2 — Una corrección se propaga (E6)

1. Entra como **luis@taxisnorte.es** (operador).
2. Anota el importe medio que muestra el panel.
3. En *Registrar corrección*: elige un viaje, campo **Importe total**, pon un
   valor claramente distinto (por ejemplo 150) y un motivo.
4. Pulsa **Aplicar corrección**: los KPI y las gráficas se actualizan solos, y
   el indicador *corregidos / cancelados* sube.
5. Pregunta al chat *"¿por qué ha cambiado esa cifra?"* → devuelve valor
   original, valor nuevo, motivo, autor y momento exacto.

### Escena 3 — Cancelación sin perder historial (E6)

1. Con el mismo usuario, pulsa **Cancelar este viaje** con un motivo.
2. El número de viajes baja en uno, pero el viaje sigue apareciendo en el
   historial de correcciones, tachado. No se ha borrado nada.

### Escena 4 — Roles y cuota (E7)

1. Como **ana** (usuario), pide *"dame las métricas globales"* → denegado.
2. Entra como **auditor@plataforma.es** y repite → ahora sí, con el desglose de
   las dos empresas.
3. Para enseñar el límite de recursos, lanza muchas consultas seguidas desde una
   empresa: a partir de la cuota responde `429` y la otra empresa sigue
   funcionando con normalidad.

---

## 3. Comprobar que funciona de verdad

Hay 22 pruebas automáticas que se ejecutan contra la API y la base de datos
reales, no contra simulaciones:

```bash
docker compose exec -e PYTHONPATH=/srv api python -m pytest /srv/tests -v
```

Las pruebas cubren, entre otras cosas:

- Que las dos empresas ven conjuntos de viajes **disjuntos**, y que sus totales
  suman exactamente lo ingerido (ni de más ni de menos).
- Que un operador **no puede corregir un viaje de otra empresa** (recibe el
  mismo 404 que si el viaje no existiera, para no revelar que existe).
- Que una corrección **conserva el valor original** con motivo, autor y momento.
- Que una cancelación **saca el viaje de las métricas pero no del historial**.
- Que la **cuota por empresa** se aplica y no afecta a las demás empresas.
- Que el **catálogo de herramientas se recorta según el rol**, y que ninguna
  expone un parámetro de empresa al modelo.
- Que **si el proveedor de LLM se cae**, la plataforma sigue respondiendo.

---

## 4. Estructura del proyecto

```
.
├── docker-compose.yml       Orquestación de los 5 servicios
├── .env.example             Plantilla de configuración
├── db/init/01_esquema.sql   Esquema, vista vigente, RLS y recálculo incremental
├── ingest/                  Carga inicial del CSV y del catálogo de zonas
│   ├── ingesta.py
│   └── datos/
│       ├── rows.csv                 999 viajes (TLC 2020)
│       └── taxi_zone_lookup.csv     265 zonas oficiales de NYC
├── api/app/
│   ├── main.py              Rutas HTTP
│   ├── auth.py              JWT, roles y verificación de contraseña (E7)
│   ├── cuotas.py            Límite de consumo por empresa (E7)
│   ├── db.py                Pool de conexiones y contexto RLS
│   ├── repositorio.py       Todo el SQL de negocio
│   ├── herramientas.py      Catálogo de funciones del chatbot, recortado por rol
│   ├── motor.py             Despachador de motor + NLU por reglas
│   ├── proveedor_openai.py  Bucle para Ollama, Groq, Gemini y OpenAI
│   └── proveedor_anthropic.py  Bucle para la API de Anthropic
├── web/index.html           Panel de chat, gráficas y correcciones
├── tests/                   22 pruebas de E6, E7 y del motor
└── docs/
    ├── arquitectura.md      Componentes y por qué cada tecnología
    ├── decisiones-e6-e7.md  Cómo se cumple cada requisito del enunciado
    └── casos-de-uso.md      Los casos de uso del chatbot
```

---

## 5. Problemas frecuentes

**`error during connect... docker_engine`** — Docker Desktop no está arrancado.
Ábrelo y espera a que ponga *Engine running*.

**`LLM_API_KEY variable is not set`** — falta el fichero `.env`. Docker Compose
solo lee un fichero llamado exactamente `.env`; `.env.example` es la plantilla.
Es solo un aviso: el chatbot arranca igual en modo reglas.

**El modo Ollama tarda mucho en la primera respuesta** — está descargando el
modelo. Míralo con `docker compose logs ollama-modelo`. Cuando ese contenedor
termine, las respuestas ya son normales.

**El panel dice que no conecta con la API** — comprueba
http://localhost:8000/salud. Si no responde, mira los registros con
`docker compose logs api`.

**Quiero empezar de cero** — `docker compose down -v` borra también el volumen
de la base de datos, y el siguiente arranque vuelve a ingerir el CSV.

---

## 6. Fuentes de datos

- **2020 Yellow Taxi Trip Data**, NYC Open Data —
  https://data.cityofnewyork.us/Transportation/2020-Yellow-Taxi-Trip-Data/kxp8-n2sj/about_data
- **Taxi Zone Lookup Table** (265 zonas), NYC TLC —
  https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page
