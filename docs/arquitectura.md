# Arquitectura de la plataforma

## Visión general

```
                        ┌──────────────────────────┐
   Navegador ─────────► │  Panel web (nginx)       │
                        │  chat + gráficas + E6    │
                        └───────────┬──────────────┘
                                    │ HTTP + JWT
                                    ▼
    ┌───────────────────────────────────────────────────────────┐
    │  API (FastAPI)                                            │
    │                                                           │
    │  1. auth.py       valida el JWT  → empresa + rol     (E7) │
    │  2. cuotas.py     límite de consumo por empresa      (E7) │
    │  3. motor.py      diálogo: LLM  ó  reglas                 │
    │  4. herramientas  catálogo recortado según el rol    (E7) │
    │  5. repositorio   SQL de negocio                     (E6) │
    └───────────┬───────────────────────────────┬───────────────┘
                │                               │
                ▼                               ▼
    ┌───────────────────────┐      ┌──────────────────────────┐
    │  Redis                │      │  PostgreSQL              │
    │  · historial de chat  │      │  · viajes (inmutable)    │
    │  · contadores cuota   │      │  · correcciones (log)    │
    └───────────────────────┘      │  · vista vigente         │
                                   │  · cubo de métricas      │
                                   │  · RLS por empresa       │
                                   └──────────▲───────────────┘
                                              │
                                   ┌──────────┴───────────────┐
                                   │  Ingesta (una vez)       │
                                   │  CSV TLC + zonas NYC     │
                                   └──────────────────────────┘
```

## Componentes del chatbot

Siguiendo el esquema de agente conversacional de la asignatura:

| Componente del esquema | Dónde está | Qué hace aquí |
|---|---|---|
| Interfaz de usuario | `web/index.html` | Chat, gráficas y formulario de correcciones |
| NLU (intención y entidades) | `motor.py`, `proveedor_*.py` | *Function calling* del LLM, o detección por palabras clave |
| Gestión del diálogo | `proveedor_*.py` + Redis | Bucle de herramientas y memoria de conversación |
| Acciones | `herramientas.py` | Catálogo de funciones, recortado por rol |
| Acceso a datos | `repositorio.py` | SQL con contexto de empresa |
| Base de conocimiento | `zonas` en Postgres | Catálogo de las 265 zonas oficiales de NYC |
| Integración | `main.py` | API HTTP con JWT y cuotas |

## Elección de tecnologías

### PostgreSQL como almacenamiento principal

Lo decisivo fue el **Row Level Security nativo**. Es lo que permite que el
aislamiento entre empresas (E7) viva en el motor y no solo en el código de la
aplicación. MongoDB o MySQL habrían obligado a confiar exclusivamente en que
ningún desarrollador olvide un filtro.

El segundo motivo es que las métricas de este escenario son agregaciones
relacionales sencillas sobre un esquema estable; no hay nada aquí que pida un
motor documental o columnar con este volumen.

### Redis como segundo almacenamiento

Dos usos, ambos mal encajados en Postgres:

- **Historial de conversación**: estado efímero con caducidad automática. No
  tiene sentido castigar la base de datos analítica con una escritura por cada
  turno de chat.
- **Contadores de cuota** (E7): necesitan operaciones atómicas de incremento y
  caducidad, que es exactamente para lo que sirve un `sorted set` de Redis.

Tener dos almacenamientos con responsabilidades distintas responde además al
criterio de *"incluya varias opciones de almacenamiento"*.

### FastAPI

Validación automática de entradas con Pydantic, documentación interactiva
generada sola (`/docs`, que sirve de prueba de concepto de la API sin escribir
un cliente), y un sistema de dependencias que encaja bien con la comprobación de
rol: `Depends(exigir_rol("operador"))` se lee como lo que hace.

### Motor de diálogo intercambiable

La decisión de fondo no fue *qué LLM usar*, sino **no atarse a ninguno**. El
motor está separado en proveedores y se elige con una variable de entorno:
`reglas`, `ollama`, `openai` (compatible: Groq, Gemini, OpenAI) o `anthropic`.

El motivo es que en los cuatro casos se usan las mismas herramientas y la misma
capa de datos. El aislamiento entre empresas y las correcciones viven en
Postgres y en `repositorio.py`, no en el modelo, así que cambiar de proveedor
**no puede romper E6 ni E7**. Esto se comprueba en las pruebas: la batería pasa
igual en modo reglas que con un proveedor de LLM configurado.

Frente a Rasa o DialogFlow:

- **DialogFlow** se descartó por ser un servicio externo gestionado: obliga a
  depender de una cuenta de Google y complica desplegar todo con un solo
  `docker compose up`.
- **Rasa** es una opción sólida y auto-alojada, pero exige entrenar un modelo de
  intenciones con ejemplos de frases; para un dominio de nueve funciones bien
  definidas, el *function calling* llega al mismo sitio sin mantener un conjunto
  de entrenamiento.
- **Function calling** permite además que el modelo encadene varias llamadas
  (*"compara la tarifa media de Manhattan con la de Brooklyn"*) sin declarar ese
  flujo de antemano.

**Ollama como opción recomendada.** Levantar el modelo en un contenedor propio
es lo más coherente con el resto del despliegue: no hace falta ninguna clave, no
hay coste, y sobre todo **los datos no salen de la plataforma**. Esto último
importa en un escenario multiempresa: con un proveedor externo, los resultados
de las consultas viajan a un tercero, lo que sería directamente incompatible con
una restricción de privacidad como E3.

**El modo reglas no es un plan B improvisado.** Comparte el catálogo de
herramientas y la capa de datos, cubre los nueve casos de uso y actúa además de
red de seguridad: si el proveedor configurado falla en caliente, la petición se
resuelve en modo reglas en lugar de devolver un error, y la respuesta indica que
hubo degradación.

## Despliegue

Cinco servicios en `docker-compose.yml`, con dependencias declaradas por estado
de salud para que el arranque sea determinista:

```
db ──(healthy)──► ingesta ──(completed)──► api ──► web
cache ──(healthy)───────────────────────────┘
ollama ──(healthy)──► ollama-modelo        (perfil opcional)
```

Ollama va en un **perfil** de Compose, así que no se levanta ni se descarga
salvo que se pida expresamente con `--profile ollama`. El arranque normal del
proyecto no paga ese coste.

La API espera a que la ingesta **termine correctamente**
(`service_completed_successfully`), no solo a que arranque. Así nunca se
levanta contra una base de datos a medio cargar, que es la causa típica de que
un `docker compose up` falle de forma intermitente.

El contenedor de la API declara además un `HEALTHCHECK` contra `/salud`, que
comprueba Postgres y Redis por separado y devuelve `503` si alguno falla.

## Alta disponibilidad

La prueba de concepto corre con una instancia de cada servicio. Los puntos
preparados para escalar, si hiciera falta justificarlo:

- La API **no guarda estado**: el historial vive en Redis y la identidad en el
  JWT. Se pueden levantar N réplicas detrás de un balanceador sin más cambios.
- Las cuotas son compartidas en Redis, así que el límite por empresa sigue
  siendo correcto con varias réplicas de API.
- Postgres admite réplicas de solo lectura; las consultas de métricas, que son
  la mayoría, podrían dirigirse a ellas.
