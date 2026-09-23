# ADR 0007 · Límite de consumo en dos capas

**Contexto.** E7: una empresa no puede consumir los recursos de las demás.

**Decisión.**
1. **nginx**, `limit_req` por token (20 pet./s, ráfaga 40): corta abusos
   groseros antes de que lleguen a Python.
2. **API + Redis**, cuota por **empresa** en ventana deslizante, con un script
   Lua atómico. Repartir el ataque entre varios usuarios de la misma empresa no
   ayuda.

**Medición** (`scripts/vecino_ruidoso.py`, 2 vCPU, generador de carga en la
misma máquina). Taxis del Norte lanza ~670 peticiones/s con 20 conexiones
concurrentes durante 20 s; Movilidad Sur hace consultas normales al mismo
tiempo.

| Configuración | Errores de la otra empresa | p95 de la otra empresa (sola → durante el ataque) |
|---|---|---|
| v4 inicial: cada 429 escribía una fila de auditoría | 0 | 23 → 187 ms |
| Auditoría una vez por ventana | 0 | 26 → 122 ms |
| + barrera en nginx (configuración final) | 0 | 21 → **66 ms** |

**Consecuencias.**
- (+) La prueba de carga destapó un fallo propio: el rechazo «barato» escribía
  en Postgres. Ahora se audita una vez por ventana y el recuento exacto va a la
  métrica `pids_cuota_rechazos_total`.
- (−) La CPU sigue siendo compartida: durante el ataque la otra empresa nota
  algo de latencia, aunque ningún error. El siguiente paso sería aislar recursos
  (varias réplicas de la API detrás de nginx, o prioridad por empresa).
