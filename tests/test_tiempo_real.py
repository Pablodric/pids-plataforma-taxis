"""
Avisos en tiempo real hacia el panel (WebSocket /ws/viajes).

Lo que se comprueba es el aislamiento (E7): el aviso solo llega a la empresa
propietaria del viaje, el ambito sale del ticket que emitio el servidor, y el
ticket es de un solo uso.
"""

import pytest
from conftest import ANA, AUDITOR, MARTA, token_de
from starlette.websockets import WebSocketDisconnect

SISTEMA_NORTE = "sistema@taxisnorte.es"
SISTEMA_SUR = "sistema@movilidadsur.es"


def viaje(**cambios):
    base = {
        "VendorID": 1, "tpep_pickup_datetime": "10/05/2026 03:10:00 PM",
        "tpep_dropoff_datetime": "10/05/2026 03:25:00 PM", "passenger_count": "1",
        "trip_distance": "2.4", "PULocationID": "161", "DOLocationID": "237",
        "payment_type": "1", "fare_amount": "11.00", "tip_amount": "2.50",
        "tolls_amount": "0.00", "congestion_surcharge": "2.50", "total_amount": "17.30",
    }
    base.update(cambios)
    return base


def url_ws(cliente, email):
    ticket = cliente.post("/ws/ticket", headers=token_de(cliente, email)).json()["ticket"]
    return f"/ws/viajes?ticket={ticket}"


def ingerir(cliente, cuenta):
    r = cliente.post("/ingesta/viajes", json=viaje(), headers=token_de(cliente, cuenta))
    assert r.status_code == 201, r.text
    return r.json()["viaje_id"]


def test_el_aviso_llega_solo_a_la_empresa_del_viaje(cliente):
    with cliente.websocket_connect(url_ws(cliente, ANA)) as ana, \
            cliente.websocket_connect(url_ws(cliente, MARTA)) as marta:
        assert ana.receive_json()["tipo"] == marta.receive_json()["tipo"] == "conectado"
        norte1 = ingerir(cliente, SISTEMA_NORTE)
        sur = ingerir(cliente, SISTEMA_SUR)
        norte2 = ingerir(cliente, SISTEMA_NORTE)
        # Ana recibe los dos de su empresa y se salta el de Sur; Marta, solo el suyo
        assert [ana.receive_json()["viaje_id"] for _ in range(2)] == [norte1, norte2]
        assert marta.receive_json()["viaje_id"] == sur


def test_el_auditor_recibe_los_avisos_de_todas_las_empresas(cliente):
    with cliente.websocket_connect(url_ws(cliente, AUDITOR)) as auditor:
        assert auditor.receive_json()["ambito"] == "*"
        ids = [ingerir(cliente, SISTEMA_NORTE), ingerir(cliente, SISTEMA_SUR)]
        assert [auditor.receive_json()["viaje_id"] for _ in range(2)] == ids


def test_el_ticket_es_de_un_solo_uso(cliente):
    url = url_ws(cliente, ANA)
    with cliente.websocket_connect(url) as ws:
        assert ws.receive_json()["tipo"] == "conectado"
    with pytest.raises(WebSocketDisconnect), cliente.websocket_connect(url):
        pass


def test_sin_ticket_valido_no_se_conecta(cliente):
    with pytest.raises(WebSocketDisconnect), cliente.websocket_connect("/ws/viajes?ticket=inventado"):
        pass
    with pytest.raises(WebSocketDisconnect), cliente.websocket_connect("/ws/viajes"):
        pass


def test_la_cuenta_de_proveedor_no_obtiene_ticket(cliente):
    r = cliente.post("/ws/ticket", headers=token_de(cliente, SISTEMA_NORTE))
    assert r.status_code == 403
    assert cliente.post("/ws/ticket").status_code == 401
