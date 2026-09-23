# Registro de decisiones de arquitectura (ADR)

Cada decisión relevante, con su contexto, las alternativas descartadas y sus
consecuencias, incluidas las negativas. Formato inspirado en
[MADR](https://adr.github.io/madr/).

| ADR | Decisión | Estado |
|---|---|---|
| [0001](0001-aislamiento-rls.md) | Tabla compartida + Row Level Security para el multiempresa | Aceptada |
| [0002](0002-log-de-eventos-inmutable.md) | Las correcciones son eventos; los hechos no se modifican | Aceptada |
| [0003](0003-cubo-incremental.md) | Métricas en un cubo materializado con recálculo incremental | Aceptada |
| [0004](0004-permisos-explicitos.md) | Permisos explícitos por rol; el auditor no escribe | Aceptada (sustituye a la jerarquía de roles de la v2) |
| [0005](0005-llm-local-como-nlu.md) | Modelo local afinado como NLU, no como agente | Aceptada (sustituye al LLM por API de la v3 como opción por defecto) |
| [0006](0006-cadena-de-hashes.md) | Cadena SHA-256 por empresa en el log de correcciones | Aceptada |
| [0007](0007-cuota-en-dos-capas.md) | Límite de consumo en dos capas: nginx por token y Redis por empresa | Aceptada |
