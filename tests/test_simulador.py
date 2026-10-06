"""
Interruptor del simulador de viajes (GET/POST /simulador).

Lo maneja el operador desde el panel y lo lee el simulador con su cuenta de
proveedor. Es por empresa (E7): encenderlo en una no lo enciende en la otra.
"""

import pytest
from conftest import ANA, AUDITOR, LUIS, MARTA, token_de

SISTEMA_NORTE = "sistema@taxisnorte.es"


@pytest.fixture(autouse=True)
def simulador_apagado_al_acabar(cliente):
    yield
    cliente.post("/simulador", json={"activo": False}, headers=token_de(cliente, LUIS))


def estado(cliente, email):
    return cliente.get("/simulador", headers=token_de(cliente, email)).json()["activo"]


def test_el_operador_enciende_y_apaga_su_empresa(cliente):
    cab = token_de(cliente, LUIS)
    assert cliente.post("/simulador", json={"activo": True}, headers=cab).json() == {"activo": True}
    assert estado(cliente, LUIS) is True
    assert cliente.post("/simulador", json={"activo": False}, headers=cab).json() == {"activo": False}
    assert estado(cliente, LUIS) is False


def test_el_interruptor_es_por_empresa(cliente):
    cliente.post("/simulador", json={"activo": True}, headers=token_de(cliente, LUIS))
    assert estado(cliente, ANA) is True           # misma empresa
    assert estado(cliente, SISTEMA_NORTE) is True  # lo que lee el simulador
    assert estado(cliente, MARTA) is False        # otra empresa: sigue apagado


@pytest.mark.parametrize("email", [ANA, AUDITOR, SISTEMA_NORTE])
def test_solo_el_operador_puede_cambiarlo(cliente, email):
    r = cliente.post("/simulador", json={"activo": True}, headers=token_de(cliente, email))
    assert r.status_code == 403
    assert estado(cliente, LUIS) is False


def test_sin_token_no_se_consulta_ni_se_cambia(cliente):
    assert cliente.get("/simulador").status_code == 401
    assert cliente.post("/simulador", json={"activo": True}).status_code == 401


def test_el_perfil_indica_quien_ve_el_boton(cliente):
    assert cliente.get("/auth/yo", headers=token_de(cliente, LUIS)).json()["puede_simular"] is True
    assert cliente.get("/auth/yo", headers=token_de(cliente, ANA)).json()["puede_simular"] is False
