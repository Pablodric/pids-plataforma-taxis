# ADR 0005 · Modelo local afinado como NLU, no como agente

**Contexto.** El chatbot necesita entender frases libres en español. La v3 usaba
un LLM por API como agente con *function calling*: tenía coste por mensaje,
dependía de la red y sacaba de la plataforma los datos de las empresas.

**Decisión.** Qwen2.5-1.5B afinado con LoRA y servido por Ollama, con un único
trabajo: convertir la frase en un JSON validado (intención y entidades). Los
permisos, las confirmaciones y las cifras siguen en código determinista.
Detalles en [`llm/README.md`](../../llm/README.md).

**Alternativas descartadas.**
- *Modelo pequeño como agente*: es poco fiable encadenando herramientas y
  puede inventarse cifras.
- *Solo reglas*: con frases no vistas aciertan el 65 % de las intenciones.

**Consecuencias.**
- (+) Local, gratis y verificable. La salida se restringe con una gramática
  (esquema JSON), se valida y se **ancla al texto**: un número de viaje que no
  aparece en el mensaje se descarta.
- (+) Degradación elegante: si el modelo falla, ese mensaje lo resuelven las reglas.
- (−) Latencia de segundos en CPU frente a milisegundos de las reglas.
- (−) Hay que mantener un dataset y reentrenar si cambian las intenciones.
