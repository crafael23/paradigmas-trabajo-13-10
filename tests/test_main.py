"""Pruebas de la conexión con el ESP32 (src/esp32.py) con ESP32 falsos en memoria."""

import json

import httpx
import pytest

from esp32 import ConexionHTTP, ConexionMQTT, SinConexion, normalizar_url, separar_broker


def esp32_falso(respuestas: list, recibidas: list):
    """Transporte httpx que responde como el servidor del ESP32."""

    def responder(peticion: httpx.Request) -> httpx.Response:
        recibidas.append(peticion)
        siguiente = respuestas.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return httpx.Response(200, json=siguiente)

    return httpx.MockTransport(responder)


def estado(ultimo_evento=0, eventos=()):
    return {"ultimo_evento": ultimo_evento, "eventos": list(eventos), "salidas": {}, "sensores": {}}


def test_normalizar_url():
    assert normalizar_url(" localhost:9080/ ") == "http://localhost:9080"
    assert normalizar_url("http://10.13.37.2") == "http://10.13.37.2"


def test_separar_broker():
    assert separar_broker("broker.hivemq.com:1883") == ("broker.hivemq.com", 1883)
    assert separar_broker("mqtt://4.tcp.ngrok.io:15432") == ("4.tcp.ngrok.io", 15432)
    assert separar_broker("localhost") == ("localhost", 1883)


async def test_sin_cambios_consulta_el_estado():
    recibidas = []
    esp = ConexionHTTP("localhost:9080", transport=esp32_falso([estado(3)], recibidas))
    datos = await esp.sincronizar()
    assert recibidas[0].method == "GET"
    assert recibidas[0].url.path == "/api/estado"
    assert esp.ultimo_evento == 3 == datos["ultimo_evento"]


async def test_los_cambios_se_agrupan_en_un_solo_post():
    recibidas = []
    esp = ConexionHTTP(transport=esp32_falso([estado(1)], recibidas))
    esp.pedir(nivel=10)
    esp.pedir(nivel=40, led=True)
    await esp.sincronizar()
    peticion = recibidas[0]
    assert peticion.method == "POST" and peticion.url.path == "/api/control"
    assert json.loads(peticion.content) == {"nivel": 40, "led": True}
    assert not esp.hay_pendientes()


async def test_si_falla_la_red_se_reintenta_sin_repetir_acciones():
    recibidas = []
    esp = ConexionHTTP(transport=esp32_falso([httpx.ConnectError("sin red"), estado()], recibidas))
    esp.pedir(led=True, accion="melodia")
    with pytest.raises(SinConexion):
        await esp.sincronizar()
    esp.pedir(nivel=70)
    await esp.sincronizar()
    assert json.loads(recibidas[1].content) == {"led": True, "nivel": 70}


async def test_detecta_reinicio_del_esp32():
    esp = ConexionHTTP(transport=esp32_falso([estado(9), estado(2, [{"id": 2}])], []))
    await esp.sincronizar()
    datos = await esp.sincronizar()
    assert datos["reiniciado"] and datos["eventos"] == [{"id": 2}]
    assert esp.ultimo_evento == 2


def test_recien_editado():
    esp = ConexionHTTP()
    assert not esp.recien_editado("led")
    esp.pedir(led=True)
    assert esp.recien_editado("led")


# ---------- MQTT ----------
class Mensaje:
    def __init__(self, topic, payload, retain=False):
        self.topic, self.payload, self.retain = topic, payload, retain


class BrokerFalso:
    """Imita lo que usa ConexionMQTT de paho.mqtt.Client."""

    def __init__(self):
        self.conectado = True
        self.publicados = []

    def is_connected(self):
        return self.conectado

    def publish(self, tema, carga):
        self.publicados.append((tema, json.loads(carga)))

        class Info:
            rc = 0

        return Info()


def llega(conexion, tema, datos, retain=False):
    carga = datos if isinstance(datos, bytes) else json.dumps(datos).encode()
    conexion._al_recibir(None, None, Mensaje(f"{conexion.tema}/{tema}", carga, retain))


async def test_mqtt_publica_comandos_en_el_tema_de_control():
    broker = BrokerFalso()
    conexion = ConexionMQTT("broker:1883", "abc", cliente=broker)
    llega(conexion, "estado", estado(1))
    conexion.pedir(led=True)
    await conexion.sincronizar()
    assert broker.publicados == [("unah/paradigmas/abc/control", {"led": True})]


async def test_mqtt_no_muestra_eventos_repetidos():
    conexion = ConexionMQTT(codigo="abc", cliente=BrokerFalso())
    llega(conexion, "estado", estado(2, [{"id": 1}, {"id": 2}]))
    assert [e["id"] for e in (await conexion.sincronizar())["eventos"]] == [1, 2]
    llega(conexion, "estado", estado(3, [{"id": 1}, {"id": 2}, {"id": 3}]))
    assert [e["id"] for e in (await conexion.sincronizar())["eventos"]] == [3]


async def test_mqtt_un_estado_retenido_viejo_no_cuenta_como_en_linea():
    conexion = ConexionMQTT(codigo="abc", cliente=BrokerFalso())
    llega(conexion, "estado", estado(1), retain=True)
    with pytest.raises(SinConexion, match="no está publicando"):
        await conexion.sincronizar()


async def test_mqtt_esp32_offline_por_ultima_voluntad():
    conexion = ConexionMQTT(codigo="abc", cliente=BrokerFalso())
    llega(conexion, "estado", estado(1))
    llega(conexion, "conexion", b"offline")
    with pytest.raises(SinConexion):
        await conexion.sincronizar()


async def test_mqtt_sin_broker():
    broker = BrokerFalso()
    broker.conectado = False
    conexion = ConexionMQTT(codigo="abc", cliente=broker)
    conexion.pedir(nivel=10)
    with pytest.raises(SinConexion, match="broker"):
        await conexion.sincronizar()
    assert conexion.hay_pendientes()
