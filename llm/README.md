# NLU con un modelo local afinado (Ollama + LoRA)

El chatbot usa un modelo de lenguaje **local**, servido por Ollama y afinado
con LoRA para esta plataforma. Sin API de pago, sin que los datos salgan de
la máquina, y con el NLU por reglas como red de seguridad.

```
                       ┌──────────────────────── llm/ (una vez, en GPU) ─────────────────────────┐
                       │ generar_dataset.py → entrenar.py (LoRA) → exportar.py (GGUF + Modelfile) │
                       └───────────────────────────────────────┬─────────────────────────────────┘
                                                               ▼  llm/modelo/pids-nlu.gguf
 mensaje ──► API ──► Ollama: pids-nlu ──► JSON {intención, viaje, campo, valor, distrito, fecha, motivo}
                        │  (esquema forzado)          │
                        │                             ▼  validar() + anclar() al texto
                        └─ si falla ──► reglas ──►  motor de diálogo: permisos, confirmación,
                                                    herramientas SQL (RLS) → respuesta con cifras reales
```

## 1. Qué hace el modelo y por qué

El modelo es el componente **NLU** del agente: convierte cada frase en un JSON
con intención y entidades (contrato en `api/app/esquema_nlu.py`). **No**
redacta las cifras ni decide los permisos: eso sigue en el código y en la
base de datos.

| Alternativa | Por qué no |
|---|---|
| Modelo pequeño como agente completo (encadena herramientas y redacta) | Con 0,5–1,5B de parámetros se equivoca al encadenar llamadas y puede inventarse cifras. Un agente así necesita un modelo grande, es decir, GPU o API de pago. |
| Modelo grande por API (versión 3) | Coste por mensaje, depende de la red y los datos de las empresas salen de la plataforma. |
| Solo reglas | Funcionan, pero no generalizan: con frases nuevas aciertan el 65 % de las intenciones (medido abajo). |
| **Modelo pequeño afinado como NLU (elegida)** | La tarea es acotada (18 intenciones y 6 entidades) y un fine-tuning corto basta. El modelo corre en CPU y su salida se puede verificar. |

### Tres protecciones sobre la salida del modelo

1. **Salida estructurada**: la API envía el esquema JSON en el campo `format`
   de Ollama, que restringe la generación con una gramática. El modelo no
   puede devolver una intención inexistente ni un JSON roto.
2. **Validación**: `esquema_nlu.validar()` descarta cualquier valor fuera del
   contrato.
3. **Anclaje al texto**: el número de viaje, el valor y el motivo tienen que
   aparecer en el mensaje (`nlu_llm.anclar()`). Si el modelo se inventa un
   viaje, se descarta y el bot pregunta en vez de preparar una corrección
   equivocada. Además, toda escritura pide confirmación al usuario.

Y si Ollama no responde, el modelo tarda demasiado o devuelve algo inválido,
ese mensaje se procesa con las reglas. La plataforma nunca depende del
modelo para funcionar.

## 2. Dataset (`generar_dataset.py`)

```bash
python llm/generar_dataset.py        # → llm/datos/{train,val,test}.jsonl
```

- Plantillas escritas a mano para las 18 intenciones, con huecos para las entidades.
  Los valores salen de los datos reales (ids de viaje, fechas del CSV) y de
  listas de motivos, distritos y sinónimos (*tarifa*, *precio*, *lo cobrado*…).
- Ruido realista: sin tildes, minúsculas, sin «¿», erratas y muletillas.
- **El test usa plantillas distintas de las del entrenamiento.** Así se mide
  si el modelo entiende frases nuevas, no si memoriza.
- Tamaño por defecto: 2.639 de entrenamiento, 278 de validación y 556 de test
  (`--factor 2` lo duplica).

## 3. Entrenamiento (`entrenar.py`)

La forma más cómoda es **Google Colab con GPU gratuita**: abre
`llm/entrenar_colab.ipynb`, sube el zip del proyecto y ejecuta las celdas.
Hace todo: dataset, entrenamiento, exportación, evaluación y descarga.

Con GPU propia:

```bash
pip install -r llm/requirements.txt
python llm/entrenar.py                                   # Qwen2.5-1.5B-Instruct
python llm/entrenar.py --modelo-base Qwen/Qwen2.5-0.5B-Instruct --epocas 3   # más ligero
```

| Parámetro | Valor | Motivo |
|---|---|---|
| Modelo base | Qwen2.5-1.5B-Instruct | Buen español, ya sabe producir JSON, Apache 2.0, ~1 GB cuantizado |
| Método | LoRA r=16, α=32, dropout 0,05 | Sobre atención y MLP (q,k,v,o,gate,up,down) |
| Pérdida | Solo sobre el JSON de la respuesta | El prompt se enmascara (−100): no se aprende a repetirlo |
| Épocas / lr | 2 / 2·10⁻⁴, coseno, 5 % calentamiento | Tarea acotada: más épocas sobreajustan a las plantillas |
| Lote efectivo | 16 (8 × 2 acumulación) | Cabe en una T4 con *gradient checkpointing* |
| Precisión | bf16 si la GPU lo admite, si no fp16 | Adaptadores siempre en fp32 |

Al terminar mide el JSON exacto sobre validación y guarda el adaptador y el
modelo fusionado en `llm/salida/`.

## 4. Exportación a Ollama (`exportar.py`)

```bash
python llm/exportar.py               # → llm/modelo/pids-nlu.gguf + Modelfile (q8_0)
python llm/exportar.py --tipo f16    # si quieres que Ollama lo cuantice a q4_K_M
```

Convierte el modelo fusionado con `convert_hf_to_gguf.py` de llama.cpp (lo
clona si hace falta) y genera un `Modelfile` con la plantilla ChatML de Qwen,
el prompt de sistema del entrenamiento y temperatura 0.

**Instalación**: con `docker compose up` es automática. El servicio
`ollama-init` crea `pids-nlu` a partir de `llm/modelo/`. Con Ollama nativo:
`cd llm/modelo && ollama create pids-nlu -f Modelfile`. Para cuantizar a
q4_K_M (~1 GB) exporta en `f16` y arranca con `CUANTIZAR=q4_K_M docker compose up`.

**Sin modelo afinado** el sistema también funciona: `ollama-init` descarga
`qwen2.5:1.5b` y la API lo usa con ejemplos de pocos disparos. El panel
indica «(base)» en la cabecera.

## 5. Evaluación (`evaluar.py`)

```bash
# Reglas vs modelo base vs modelo afinado, sobre las 556 frases de test
python llm/evaluar.py --motor reglas \
    --motor ollama --modelo qwen2.5:1.5b --base qwen2.5:1.5b \
    --motor ollama --modelo pids-nlu --url http://localhost:11435 \
    --json llm/modelo/informe_evaluacion.json
```

Mide el acierto y la F1 macro de intención, el acierto por entidad, el
**marco exacto** (todo el JSON correcto, lo necesario para preparar una
corrección sin preguntar) y la latencia.

### Resultados

| Motor | Intención | F1 macro | Marco exacto | Estado |
|---|---|---|---|---|
| Reglas (`nlu.py`) | 65,3 % | 0,577 | 62,6 % | **Medido** |
| Qwen2.5-1.5B base + pocos disparos | — | — | — | Pendiente: ejecutar el cuaderno |
| Qwen2.5-1.5B + LoRA (`pids-nlu`) | — | — | — | Pendiente: ejecutar el cuaderno |

Las reglas fallan sobre todo en paráfrasis que no están en sus patrones
(«ponme las empresas una al lado de otra», «necesito arreglar la propina del
viaje 82», «¿dónde recogemos más clientes?»). Es justo lo que se espera que
resuelva el fine-tuning. El cuaderno de Colab rellena las dos filas que
faltan en `llm/modelo/informe_evaluacion.json`.

### Verificación del pipeline sin GPU

En el entorno de desarrollo no había GPU ni acceso a los pesos de Qwen. Para
comprobar que el pipeline funciona de principio a fin se usó un modelo Qwen2
diminuto generado en local (1,7 M de parámetros, pesos aleatorios, tokenizador
BPE propio con plantilla ChatML), con el mismo código:

- Máscara de la pérdida verificada: las etiquetas decodificadas son
  exactamente `{"intencion": …}<|im_end|>`.
- Entrenamiento completo, 3 épocas en CPU: la pérdida baja de ~7 a 0,16 (en
  validación) y el **95 % de las salidas son JSON válido**. El acierto exacto
  es bajo (7,5 %), lo esperado en un modelo que no sabe español. Precisamente
  por eso se parte de un modelo preentrenado.
- LoRA, *gradient checkpointing*, fusión y guardado: ejecutados.
- Exportación a GGUF (arquitectura `qwen2`, tensores Q8_0, plantilla ChatML),
  servida con llama.cpp (el motor de Ollama). **Con el esquema como gramática,
  el 100 % de las salidas (40/40) son JSON válido**, frente al 95 % sin ella.
  Esto confirma además que el esquema, con enumeraciones que admiten `null`,
  se traduce bien a gramática.
- Cadena completa con ese GGUF detrás de la API de Ollama: `evaluar.py` y el
  `/chat` de la API funcionan en modo `ollama:pids-nlu`, y `/salud` informa
  del modelo afinado.
- Integración con la API: 17 pruebas contra un servidor que habla el
  protocolo HTTP de Ollama (`tests/test_ollama.py`).

Lo que **no** se ha podido ejecutar aquí es el entrenamiento sobre el Qwen2.5
real ni la imagen Docker de Ollama: se hace con el cuaderno de Colab.

## 6. Compartir el modelo afinado

El `.gguf` (~1,6 GB en q8_0) **no va en el repositorio**: GitHub rechaza
ficheros de más de 100 MB, y `.gitignore` ya lo excluye. Publícalo como
*Release*, que admite ficheros de hasta 2 GB:

1. En GitHub: **Releases → Draft a new release**, etiqueta `modelo-v1`.
2. Adjunta `pids-nlu.gguf`, `Modelfile` e `informe_evaluacion.json`.
3. Quien clone el proyecto descarga esos ficheros en `llm/modelo/` y hace
   `docker compose up`.

(Alternativa: subirlo a Hugging Face, que está pensado para modelos.)

## 7. Problemas frecuentes

**El panel dice «(base)»**: no hay `pids-nlu.gguf` en `llm/modelo/`. Entrena
con el cuaderno y copia ahí los ficheros.

**Las respuestas tardan varios segundos**: en CPU, un modelo de 1,5B tarda
bastante más en cada mensaje que las reglas, que son instantáneas. Opciones:
- el modelo de 0,5B;
- cuantizar a q4_K_M;
- en un Mac, Ollama nativo (usa la GPU de Apple) con
  `OLLAMA_URL=http://host.docker.internal:11434 docker compose up`;
- en Linux con NVIDIA, `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up`.

**Quiero volver a las reglas**: `LLM_PROVEEDOR=reglas docker compose up`.

**`ollama create` falla con `--quantize`**: Ollama solo cuantiza desde f16 o
f32. Exporta con `--tipo f16`.
