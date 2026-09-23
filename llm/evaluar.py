"""
Evalua un NLU sobre el conjunto de test (frases de plantillas que el modelo
no vio al entrenar) y compara motores.

    python llm/evaluar.py --motor reglas
    python llm/evaluar.py --motor ollama --modelo pids-nlu              # afinado

    # Comparativa completa: reglas vs modelo base vs modelo afinado
    python llm/evaluar.py --motor reglas \
        --motor ollama --modelo qwen2.5:1.5b --base qwen2.5:1.5b \
        --motor ollama --modelo pids-nlu --json llm/informe_evaluacion.json

Metricas:
  - intencion: acierto y F1 macro (todas las intenciones pesan igual)
  - por entidad: acierto de viaje_id, campo, valor, borough, fecha, motivo
    sobre los ejemplos donde la entidad existe o el motor la inventa
  - marco exacto: TODO el JSON correcto (lo que de verdad importa para
    ejecutar una correccion sin preguntar)
  - latencia media y p95 por mensaje
"""

import argparse
import json
import statistics
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "api"))
from app import esquema_nlu as E  # noqa: E402

TEST = Path(__file__).resolve().parent / "datos" / "test_etiquetado.jsonl"
ENTIDADES = ["viaje_id", "campo", "valor", "borough", "fecha", "motivo"]


def _norm(v):
    if isinstance(v, str):
        v = "".join(c for c in unicodedata.normalize("NFD", v.lower()) if unicodedata.category(c) != "Mn")
        return " ".join(v.strip(" .,;").split())
    if isinstance(v, float):
        return round(v, 2)
    return v


def iguales(clave, a, b) -> bool:
    if clave == "valor" and a is not None and b is not None:
        return abs(float(a) - float(b)) < 0.005
    return _norm(a) == _norm(b)


def motor_reglas():
    from app import nlu

    def predecir(texto):
        return nlu.a_marco(nlu.interpretar(texto))
    return predecir


def motor_ollama(url: str, modelo: str, base: bool = False):
    """base=True: modelo sin afinar, se le dan los ejemplos de pocos disparos
    que usa la API cuando el modelo afinado no esta instalado."""
    from app import nlu_llm

    cliente = nlu_llm.ClienteOllama(url, modelo, timeout=120)

    def predecir(texto):
        return cliente.interpretar(texto, modelo, afinado=not base)
    return predecir


def evaluar(predecir, ejemplos, limite=None, verbose=False):
    ejemplos = ejemplos[:limite] if limite else ejemplos
    aciertos_int, exactos, latencias, errores = 0, 0, [], []
    ent_ok, ent_total = Counter(), Counter()
    por_intencion = defaultdict(lambda: Counter())  # tp, fp, fn
    fallos_json = 0
    for i, ej in enumerate(ejemplos, 1):
        esperado = ej["esperado"]
        t0 = time.perf_counter()
        try:
            pred = predecir(ej["texto"])
        except Exception as exc:  # salida no valida: cuenta como fallo total
            fallos_json += 1
            pred = E.vacio("fuera_de_dominio")
            if verbose:
                print(f"  ! {ej['texto']!r}: {exc}")
        latencias.append((time.perf_counter() - t0) * 1000)

        ok_int = pred["intencion"] == esperado["intencion"]
        aciertos_int += ok_int
        if ok_int:
            por_intencion[esperado["intencion"]]["tp"] += 1
        else:
            por_intencion[esperado["intencion"]]["fn"] += 1
            por_intencion[pred["intencion"]]["fp"] += 1
        todo = ok_int
        for k in ENTIDADES:
            if esperado.get(k) is None and pred.get(k) is None:
                continue
            ent_total[k] += 1
            if iguales(k, pred.get(k), esperado.get(k)):
                ent_ok[k] += 1
            else:
                todo = False
        exactos += todo
        if not todo:
            errores.append({"texto": ej["texto"], "esperado": esperado, "obtenido": pred})
        if verbose and i % 50 == 0:
            print(f"  {i}/{len(ejemplos)}")

    f1s = []
    for c in por_intencion.values():
        p = c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else 0
        r = c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else 0
        f1s.append(2 * p * r / (p + r) if p + r else 0)
    n = len(ejemplos)
    return {
        "ejemplos": n,
        "intencion_acierto": round(aciertos_int / n, 4),
        "intencion_f1_macro": round(statistics.mean(f1s), 4) if f1s else 0,
        "entidades_acierto": {k: round(ent_ok[k] / ent_total[k], 4) for k in ENTIDADES if ent_total[k]},
        "marco_exacto": round(exactos / n, 4),
        "salidas_no_validas": fallos_json,
        "latencia_ms_media": round(statistics.mean(latencias), 1),
        "latencia_ms_p95": round(sorted(latencias)[int(.95 * (n - 1))], 1),
        "errores": errores,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--motor", choices=["reglas", "ollama"], action="append",
                    help="se puede repetir para comparar")
    ap.add_argument("--modelo", action="append", default=[],
                    help="modelo de Ollama (repetible, uno por cada --motor ollama)")
    ap.add_argument("--base", action="append", default=[],
                    help="modelo(s) de --modelo que son BASE (sin afinar): se les dan ejemplos")
    ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--test", type=Path, default=TEST)
    ap.add_argument("--limite", type=int)
    ap.add_argument("--errores", type=int, default=8, help="cuantos errores mostrar")
    ap.add_argument("--json", type=Path, help="guardar el informe completo")
    a = ap.parse_args()
    motores = a.motor or ["reglas"]

    with open(a.test, encoding="utf-8") as f:
        ejemplos = [json.loads(linea) for linea in f]
    informe, modelos = {}, iter(a.modelo)
    for m in motores:
        if m == "reglas":
            nombre, pred = "reglas", motor_reglas()
        else:
            modelo = next(modelos, "pids-nlu")
            nombre, pred = f"ollama:{modelo}", motor_ollama(a.url, modelo, modelo in a.base)
        print(f"== {nombre} ({len(ejemplos[:a.limite] if a.limite else ejemplos)} ejemplos)")
        r = evaluar(pred, ejemplos, a.limite, verbose=m != "reglas")
        informe[nombre] = r
        print(json.dumps({k: v for k, v in r.items() if k != "errores"}, ensure_ascii=False, indent=2))
        for e in r["errores"][:a.errores]:
            dif = {k: (e["esperado"][k], e["obtenido"].get(k)) for k in E.CLAVES
                   if not iguales(k, e["esperado"][k], e["obtenido"].get(k))}
            print(f"   ✗ {e['texto']!r}  {dif}")

    if len(informe) > 1:
        print("\nResumen             intención   F1 macro   marco exacto   latencia")
        for nombre, r in informe.items():
            print(f"{nombre:18s} {r['intencion_acierto']:9.1%} {r['intencion_f1_macro']:10.3f}"
                  f" {r['marco_exacto']:13.1%} {r['latencia_ms_media']:8.0f} ms")
    if a.json:
        a.json.write_text(json.dumps(informe, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
