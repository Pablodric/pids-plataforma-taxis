"""
Servidor que imita la API HTTP de Ollama (/api/tags y /api/chat) para las
pruebas: permite comprobar la integracion sin descargar un modelo.

Por defecto "responde" como un modelo perfecto usando el NLU por reglas,
pero se le puede programar una respuesta concreta (JSON invalido, un viaje
inventado...) y guarda las peticiones recibidas para inspeccionarlas.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class OllamaSimulado:
    def __init__(self, modelos=("pids-nlu:latest", "qwen2.5:1.5b")):
        self.modelos = list(modelos)
        self.peticiones: list[dict] = []
        self.respuesta_fija: str | None = None   # contenido literal a devolver
        self.estado_http = 200
        simulado = self

        class Manejador(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, codigo, cuerpo):
                datos = json.dumps(cuerpo).encode()
                self.send_response(codigo)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._json(200, {"models": [{"name": m} for m in simulado.modelos]})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                cuerpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                simulado.peticiones.append(cuerpo)
                if self.path != "/api/chat":
                    return self._json(404, {"error": "not found"})
                if simulado.estado_http != 200:
                    return self._json(simulado.estado_http, {"error": "simulado"})
                nombres = {m.split(":")[0] for m in simulado.modelos} | set(simulado.modelos)
                if cuerpo["model"] not in nombres:
                    return self._json(404, {"error": f"model '{cuerpo['model']}' not found"})
                contenido = simulado.respuesta_fija
                if contenido is None:
                    from app import esquema_nlu, nlu
                    texto = cuerpo["messages"][-1]["content"]
                    contenido = esquema_nlu.serializar(nlu.a_marco(nlu.interpretar(texto)))
                self._json(200, {
                    "model": cuerpo["model"],
                    "message": {"role": "assistant", "content": contenido},
                    "done": True,
                })

        self.servidor = ThreadingHTTPServer(("127.0.0.1", 0), Manejador)
        self.url = f"http://127.0.0.1:{self.servidor.server_address[1]}"
        self.hilo = threading.Thread(target=self.servidor.serve_forever, daemon=True)

    def __enter__(self):
        self.hilo.start()
        return self

    def __exit__(self, *exc):
        self.servidor.shutdown()
        self.servidor.server_close()
