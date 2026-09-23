"""
Fine-tuning LoRA del NLU sobre Qwen2.5-Instruct.

    # GPU (Colab T4 gratuita, ~15-20 min con 1,5B y 2 epocas):
    python llm/entrenar.py

    # Otro tamano o menos pasos:
    python llm/entrenar.py --modelo-base Qwen/Qwen2.5-0.5B-Instruct --epocas 3

Que hace:
  1. Lee llm/datos/{train,val}.jsonl (lo genera generar_dataset.py).
  2. Tokeniza con la plantilla de chat del modelo y ENMASCARA el prompt:
     la perdida solo se calcula sobre el JSON de la respuesta. El modelo no
     gasta capacidad en aprender a repetir el prompt de sistema.
  3. Entrena adaptadores LoRA (r=16 sobre atencion y MLP): ~1 % de los
     parametros, cabe en una GPU gratuita.
  4. Guarda el adaptador y el modelo FUSIONADO (base + LoRA) en
     llm/salida/fusionado, listo para exportar a GGUF (exportar.py).
  5. Mide el acierto de JSON exacto sobre validacion con decodificacion
     voraz, como comprobacion rapida (la evaluacion seria es evaluar.py).

Por que Qwen2.5-1.5B-Instruct: buen espanol, ya sabe producir JSON, licencia
Apache 2.0, y cuantizado cabe en ~1 GB y responde en CPU en 1-3 s.
"""

import argparse
import inspect
import json
import math
import sys
import time
from pathlib import Path

import torch
from torch.utils.data import Dataset

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI.parent / "api"))


def cargar_jsonl(ruta: Path) -> list[dict]:
    with open(ruta, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


class DatasetChat(Dataset):
    """Conversaciones {system,user,assistant} -> input_ids + labels con el
    prompt enmascarado (-100)."""

    def __init__(self, ejemplos, tok, max_len):
        self.items = []
        truncados = 0
        for ej in ejemplos:
            mensajes = ej["messages"]
            prompt = tok.apply_chat_template(mensajes[:-1], tokenize=False,
                                             add_generation_prompt=True)
            respuesta = mensajes[-1]["content"] + FIN_TURNO(tok)
            ids_prompt = tok(prompt, add_special_tokens=False)["input_ids"]
            ids_resp = tok(respuesta, add_special_tokens=False)["input_ids"]
            ids = ids_prompt + ids_resp
            if len(ids) > max_len:
                truncados += 1
                continue
            self.items.append({
                "input_ids": ids,
                "labels": [-100] * len(ids_prompt) + ids_resp,
            })
        if truncados:
            print(f"[datos] {truncados} ejemplos descartados por superar {max_len} tokens")

    def __len__(self):
        return len(self.items)

    def __getitem__(self, i):
        return self.items[i]


def FIN_TURNO(tok) -> str:
    """Token de fin de turno de la plantilla (Qwen: <|im_end|>). Se entrena
    para que el modelo aprenda a PARAR tras el JSON."""
    for candidato in ("<|im_end|>", "<|eot_id|>", "<end_of_turn>"):
        if candidato in tok.get_vocab():
            return candidato
    return tok.eos_token


class Colador:
    def __init__(self, pad_id):
        self.pad_id = pad_id

    def __call__(self, lote):
        largo = max(len(x["input_ids"]) for x in lote)
        ids, labels, mascara = [], [], []
        for x in lote:
            relleno = largo - len(x["input_ids"])
            ids.append(x["input_ids"] + [self.pad_id] * relleno)
            labels.append(x["labels"] + [-100] * relleno)
            mascara.append([1] * len(x["input_ids"]) + [0] * relleno)
        return {"input_ids": torch.tensor(ids), "labels": torch.tensor(labels),
                "attention_mask": torch.tensor(mascara)}


def tipo_de_datos(forzar_cpu: bool):
    if forzar_cpu or not torch.cuda.is_available():
        return torch.float32, {}
    if torch.cuda.is_bf16_supported():
        return torch.bfloat16, {"bf16": True}
    return torch.float16, {"fp16": True}


def cargar_modelo(nombre, dtype):
    from transformers import AutoModelForCausalLM

    # transformers 5 renombro torch_dtype -> dtype; se admite cualquiera
    params = inspect.signature(AutoModelForCausalLM.from_pretrained).parameters
    clave = "dtype" if "dtype" in params or _es_v5() else "torch_dtype"
    return AutoModelForCausalLM.from_pretrained(nombre, **{clave: dtype})


def _es_v5() -> bool:
    import transformers
    return int(transformers.__version__.split(".")[0]) >= 5


@torch.no_grad()
def acierto_json(modelo, tok, ejemplos, n, max_nuevos=160) -> dict:
    """Decodificacion voraz sobre n ejemplos: % JSON valido y % exacto."""
    from app import esquema_nlu as E

    modelo.eval()
    validos = exactos = 0
    muestra = ejemplos[:n]
    for ej in muestra:
        prompt = tok.apply_chat_template(ej["messages"][:-1], tokenize=False,
                                         add_generation_prompt=True)
        entrada = tok(prompt, return_tensors="pt", add_special_tokens=False).to(modelo.device)
        salida = modelo.generate(**entrada, max_new_tokens=max_nuevos, do_sample=False,
                                 pad_token_id=tok.pad_token_id,
                                 eos_token_id=tok.convert_tokens_to_ids(FIN_TURNO(tok)))
        texto = tok.decode(salida[0][entrada["input_ids"].shape[1]:], skip_special_tokens=True)
        try:
            marco = E.validar(json.loads(texto.strip()))
            validos += 1
            exactos += marco == E.validar(json.loads(ej["messages"][-1]["content"]))
        except (ValueError, json.JSONDecodeError):
            pass
    return {"n": len(muestra), "json_valido": validos / max(1, len(muestra)),
            "json_exacto": exactos / max(1, len(muestra))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modelo-base", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--datos", type=Path, default=AQUI / "datos")
    ap.add_argument("--salida", type=Path, default=AQUI / "salida")
    ap.add_argument("--epocas", type=float, default=2)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--lote", type=int, default=8)
    ap.add_argument("--acumulacion", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=768)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--max-pasos", type=int, default=-1, help="corta el entrenamiento (pruebas)")
    ap.add_argument("--n-val-generacion", type=int, default=60,
                    help="ejemplos de validacion para medir JSON exacto al final (0 = no)")
    ap.add_argument("--completo", action="store_true",
                    help="ajuste completo de todos los pesos en vez de LoRA (mas memoria)")
    ap.add_argument("--checkpointing", choices=["auto", "si", "no"], default="auto",
                    help="gradient checkpointing: menos memoria, algo mas lento (auto = si en GPU)")
    ap.add_argument("--cpu", action="store_true", help="forzar CPU (solo para pruebas)")
    ap.add_argument("--sin-fusionar", action="store_true", help="guardar solo el adaptador")
    ap.add_argument("--semilla", type=int, default=42)
    a = ap.parse_args()

    from peft import LoraConfig, get_peft_model
    from transformers import AutoTokenizer, Trainer, TrainingArguments, set_seed

    set_seed(a.semilla)
    t0 = time.time()
    dtype, precision = tipo_de_datos(a.cpu)
    dispositivo = "GPU" if torch.cuda.is_available() and not a.cpu else "CPU"
    print(f"[modelo] {a.modelo_base} ({dtype}, {dispositivo})")

    tok = AutoTokenizer.from_pretrained(a.modelo_base)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    # Ajuste completo en fp16: pesos maestros en fp32 (precision mixta), si
    # no el escalador de gradientes de fp16 falla
    modelo = cargar_modelo(a.modelo_base,
                           torch.float32 if a.completo and dtype == torch.float16 else dtype)
    if torch.cuda.is_available() and not a.cpu:
        modelo.to("cuda")
    modelo.config.use_cache = False

    lora = LoraConfig(
        r=a.lora_r, lora_alpha=2 * a.lora_r, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    en_gpu = torch.cuda.is_available() and not a.cpu
    if a.checkpointing == "si" or (a.checkpointing == "auto" and en_gpu):
        modelo.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        modelo.enable_input_require_grads()
        print("[modelo] gradient checkpointing activado")
    if a.completo:
        print(f"[modelo] ajuste completo: {sum(p.numel() for p in modelo.parameters()):,} parametros")
    else:
        modelo = get_peft_model(modelo, lora)
        # Los adaptadores se entrenan en fp32 aunque la base este en fp16/bf16:
        # estabilidad numerica y compatibilidad con el escalador de fp16 (T4)
        for p in modelo.parameters():
            if p.requires_grad:
                p.data = p.data.float()
        modelo.print_trainable_parameters()

    train_raw = cargar_jsonl(a.datos / "train.jsonl")
    val_raw = cargar_jsonl(a.datos / "val.jsonl")
    train = DatasetChat(train_raw, tok, a.max_len)
    val = DatasetChat(val_raw, tok, a.max_len)
    largos = sorted(len(x["input_ids"]) for x in train.items)
    print(f"[datos] train={len(train)} val={len(val)} tokens/ejemplo: "
          f"mediana={largos[len(largos) // 2]} max={largos[-1]}")

    pasos_epoca = math.ceil(len(train) / (a.lote * a.acumulacion))
    args = dict(
        output_dir=str(a.salida / "checkpoints"),
        per_device_train_batch_size=a.lote,
        per_device_eval_batch_size=a.lote,
        gradient_accumulation_steps=a.acumulacion,
        num_train_epochs=a.epocas,
        max_steps=a.max_pasos,
        learning_rate=a.lr,
        lr_scheduler_type="cosine",
        warmup_steps=max(1, int(0.05 * pasos_epoca * a.epocas)),
        weight_decay=0.0,
        logging_steps=max(1, min(10, pasos_epoca // 5)),
        save_strategy="no",
        report_to="none",
        remove_unused_columns=False,
        seed=a.semilla,
        use_cpu=a.cpu or not torch.cuda.is_available(),
        **precision,
    )
    # Nombre del parametro de evaluacion segun la version de transformers
    clave_eval = ("eval_strategy" if "eval_strategy" in inspect.signature(TrainingArguments).parameters
                  else "evaluation_strategy")
    args[clave_eval] = "epoch" if a.max_pasos < 0 else "no"

    entrenador = Trainer(model=modelo, args=TrainingArguments(**args), train_dataset=train,
                         eval_dataset=val, data_collator=Colador(tok.pad_token_id))
    resultado = entrenador.train()
    perdida_val = entrenador.evaluate()["eval_loss"] if len(val) else None
    print(f"[entrenamiento] perdida final={resultado.training_loss:.4f} "
          f"validacion={perdida_val if perdida_val is None else round(perdida_val, 4)} "
          f"({(time.time() - t0) / 60:.1f} min)")

    a.salida.mkdir(parents=True, exist_ok=True)
    if not a.completo:
        modelo.save_pretrained(a.salida / "adaptador")
        tok.save_pretrained(a.salida / "adaptador")

    informe = {"modelo_base": a.modelo_base, "perdida_train": resultado.training_loss,
               "perdida_val": perdida_val, "ejemplos_train": len(train),
               "minutos": round((time.time() - t0) / 60, 1)}
    if a.n_val_generacion:
        modelo.config.use_cache = True
        informe["validacion_generacion"] = acierto_json(modelo, tok, val_raw, a.n_val_generacion)
        print(f"[validacion] {informe['validacion_generacion']}")

    if not a.sin_fusionar:
        fusionado = modelo if a.completo else modelo.merge_and_unload()
        fusionado.config.use_cache = True
        fusionado.save_pretrained(a.salida / "fusionado", safe_serialization=True)
        tok.save_pretrained(a.salida / "fusionado")
        print(f"[salida] modelo fusionado en {a.salida / 'fusionado'}")
    (a.salida / "informe_entrenamiento.json").write_text(json.dumps(informe, indent=2))
    print("[listo] siguiente paso: python llm/exportar.py")


if __name__ == "__main__":
    main()
