"""
Fixtures compartidas de la bateria de pruebas.

Las pruebas se ejecutan contra la API real (FastAPI TestClient), el
Postgres real y el Redis real: nada de dobles, salvo el cliente del LLM
en las pruebas del modo LLM (no se debe gastar credito ni depender de
la red para comprobar la gestion del historial).
"""

import pytest
from app.main import app
from fastapi.testclient import TestClient

CLAVE = "demo1234"
ANA = "ana@taxisnorte.es"          # taxis_norte, usuario
LUIS = "luis@taxisnorte.es"        # taxis_norte, operador
MARTA = "marta@movilidadsur.es"    # movilidad_sur, usuario
PABLO = "pablo@movilidadsur.es"    # movilidad_sur, operador
AUDITOR = "auditor@plataforma.es"  # plataforma, auditor (solo lectura)


@pytest.fixture(scope="session")
def cliente():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def motor_por_reglas(monkeypatch):
    """
    Las pruebas son deterministas: por defecto el chat usa las reglas aunque
    el contenedor tenga LLM_PROVEEDOR=ollama. Las pruebas de Ollama y del
    modo LLM lo activan explicitamente con su servidor simulado.
    """
    from app import config

    monkeypatch.setattr(config, "OLLAMA_ACTIVO", False)
    monkeypatch.setattr(config, "LLM_ACTIVO", False)


@pytest.fixture(autouse=True)
def limpiar_cuotas(cliente):
    """
    Cada prueba arranca con la cuota de todas las empresas a cero.
    Sin esto las pruebas se estorban entre si: el limitador es por
    empresa, no por prueba, y una prueba previa agotaria la cuota de
    la siguiente.
    """
    from app import db

    for patron in ("cuota:*", "login:*"):
        for clave in db.cache().scan_iter(patron):
            db.cache().delete(clave)
    yield


def viaje_disponible(cliente, cabecera):
    """Primer viaje ni cancelado ni corregido: las pruebas no deben
    depender del estado que hayan dejado las anteriores."""
    viajes = cliente.get("/datos/viajes?limite=50", headers=cabecera).json()["viajes"]
    for v in viajes:
        if not v["cancelado"] and not v["corregido"]:
            return v
    for v in viajes:
        if not v["cancelado"]:
            return v
    raise AssertionError("No hay viajes disponibles para la prueba")


def token_de(cliente, email):
    r = cliente.post("/auth/login", json={"email": email, "password": CLAVE})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}
