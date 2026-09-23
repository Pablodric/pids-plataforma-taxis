Aquí va el modelo afinado exportado por `llm/exportar.py`:

    pids-nlu.gguf
    Modelfile

Si esta carpeta solo contiene este fichero, docker compose usa el modelo
base `qwen2.5:1.5b` (sin afinar) y lo indica en el panel. Ver `llm/README.md`.
