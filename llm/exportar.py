"""
Exporta el modelo afinado a GGUF y genera el Modelfile de Ollama.

    python llm/exportar.py                      # q8_0 (buena calidad, ~1,6 GB con 1,5B)
    python llm/exportar.py --tipo f16           # sin perdida; cuantiza luego Ollama
    python llm/exportar.py --llama-cpp ~/llama.cpp

Deja en llm/modelo/:
    pids-nlu.gguf   el modelo (base + LoRA fusionados)
    Modelfile       plantilla ChatML, prompt de sistema y parametros

Instalacion en Ollama:
    - con docker compose: automatica (servicio ollama-init) al arrancar;
    - con Ollama nativo:  cd llm/modelo && ollama create pids-nlu -f Modelfile
      (anade  --quantize q4_K_M  si exportaste en f16 y quieres ~1 GB)

La conversion usa convert_hf_to_gguf.py de llama.cpp. Si no se indica una
copia local, se clona el repositorio (solo hace falta el script de Python
y el paquete 'gguf', no compilar nada).
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "api"))
from app import esquema_nlu as E  # noqa: E402

REPO_LLAMA_CPP = "https://github.com/ggml-org/llama.cpp"

# Plantilla ChatML (la de Qwen2.5) en la sintaxis de plantillas de Ollama.
PLANTILLA = '''{{- range .Messages }}<|im_start|>{{ .Role }}
{{ .Content }}<|im_end|>
{{ end }}<|im_start|>assistant
'''


def modelfile(gguf: str, cuantizacion: str) -> str:
    sistema = E.PROMPT_SISTEMA.replace('"""', "'''")
    return f'''# Modelfile generado por llm/exportar.py
# NLU de la plataforma de taxis: Qwen2.5-Instruct afinado con LoRA ({cuantizacion}).
# Instalar:  ollama create pids-nlu -f Modelfile
# Probar:    ollama run pids-nlu "el viaje 411 tenía mal la tarifa, eran 23,50"

FROM ./{gguf}

TEMPLATE """{PLANTILLA}"""

# Prompt con el que se entreno. La API lo envia igualmente en cada peticion;
# aqui sirve para probar el modelo directamente con 'ollama run'.
SYSTEM """{sistema}"""

PARAMETER temperature 0
PARAMETER num_ctx 2048
PARAMETER num_predict 160
PARAMETER stop "<|im_end|>"
PARAMETER stop "<|im_start|>"
'''


def localizar_llama_cpp(ruta: Path | None) -> Path:
    candidatos = [ruta] if ruta else [AQUI / "llama.cpp", Path.home() / "llama.cpp"]
    for c in candidatos:
        if c and (c / "convert_hf_to_gguf.py").exists():
            return c
    destino = ruta or AQUI / "llama.cpp"
    print(f"[exportar] clonando llama.cpp en {destino} ...")
    subprocess.run(["git", "clone", "--depth", "1", REPO_LLAMA_CPP, str(destino)], check=True)
    return destino


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fusionado", type=Path, default=AQUI / "salida" / "fusionado")
    ap.add_argument("--destino", type=Path, default=AQUI / "modelo")
    ap.add_argument("--tipo", default="q8_0", choices=["q8_0", "f16", "bf16", "f32"])
    ap.add_argument("--llama-cpp", type=Path)
    ap.add_argument("--nombre", default="pids-nlu.gguf")
    a = ap.parse_args()

    if not (a.fusionado / "config.json").exists():
        sys.exit(f"No encuentro el modelo fusionado en {a.fusionado}. Ejecuta antes llm/entrenar.py")

    llama = localizar_llama_cpp(a.llama_cpp)
    a.destino.mkdir(parents=True, exist_ok=True)
    salida = a.destino / a.nombre
    cmd = [sys.executable, str(llama / "convert_hf_to_gguf.py"), str(a.fusionado),
           "--outfile", str(salida), "--outtype", a.tipo]
    print("[exportar]", " ".join(cmd))
    subprocess.run(cmd, check=True)

    (a.destino / "Modelfile").write_text(modelfile(a.nombre, a.tipo), encoding="utf-8")
    informe = a.fusionado.parent / "informe_entrenamiento.json"
    if informe.exists():
        shutil.copy(informe, a.destino / "informe_entrenamiento.json")
    (a.destino / "exportacion.json").write_text(json.dumps({
        "gguf": a.nombre, "tipo": a.tipo, "tamano_mb": round(salida.stat().st_size / 2**20, 1),
        "origen": str(a.fusionado)}, indent=2))
    print(f"[exportar] {salida} ({salida.stat().st_size / 2**20:.0f} MB) y Modelfile listos")
    print("[exportar] siguiente paso: docker compose up (se instala solo) "
          "o  cd llm/modelo && ollama create pids-nlu -f Modelfile")


if __name__ == "__main__":
    main()
