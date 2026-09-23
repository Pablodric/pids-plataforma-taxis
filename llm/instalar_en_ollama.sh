#!/bin/sh
# Instala los modelos en el servidor Ollama. Lo ejecuta el servicio
# 'ollama-init' de docker compose (una vez, al arrancar), pero tambien
# sirve a mano:  OLLAMA_HOST=http://localhost:11434 sh llm/instalar_en_ollama.sh
#
#  1. Si existe el modelo afinado (llm/modelo/pids-nlu.gguf), crea 'pids-nlu'.
#  2. Si no existe, descarga el modelo base de respaldo (qwen2.5:1.5b) para
#     que el chatbot use un LLM desde el primer dia, con pocos disparos.
# La API no espera a este proceso: mientras tanto el chat usa las reglas.
set -e

DIR_MODELO="${DIR_MODELO:-/modelo}"
MODELO="${OLLAMA_MODELO:-pids-nlu}"
RESPALDO="${OLLAMA_MODELO_RESPALDO:-qwen2.5:1.5b}"

echo "[ollama-init] esperando al servidor Ollama en ${OLLAMA_HOST:-localhost:11434}..."
i=0
until ollama list >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -gt 60 ] && { echo "[ollama-init] Ollama no responde"; exit 1; }
  sleep 2
done

if [ -f "$DIR_MODELO/Modelfile" ] && ls "$DIR_MODELO"/*.gguf >/dev/null 2>&1; then
  echo "[ollama-init] creando el modelo afinado '$MODELO' desde $DIR_MODELO"
  cd "$DIR_MODELO"
  if [ -n "$CUANTIZAR" ]; then
    ollama create "$MODELO" -f Modelfile --quantize "$CUANTIZAR"
  else
    ollama create "$MODELO" -f Modelfile
  fi
  echo "[ollama-init] '$MODELO' instalado"
  [ "${DESCARGAR_RESPALDO:-0}" = "1" ] && ollama pull "$RESPALDO"
else
  echo "[ollama-init] no hay modelo afinado en $DIR_MODELO (ver llm/README.md)."
  echo "[ollama-init] descargando el modelo base '$RESPALDO' como respaldo (~1 GB, solo la primera vez)..."
  ollama pull "$RESPALDO"
fi
ollama list
echo "[ollama-init] listo"
