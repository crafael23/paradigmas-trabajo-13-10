"""Ejecuta el firmware de Wokwi (wokwi/main.py) en la PC, sin cambios.

Sirve para probar la app de Flet sin abrir Wokwi: imita los módulos de
MicroPython (machine, network, dht, neopixel, framebuf, umqtt) y corre el
mismo firmware: servidor HTTP en un puerto local y MQTT contra el broker
configurado en wokwi/main.py (o en la variable MQTT_BROKER).

    uv run python tools/esp32_en_pc.py            # http://localhost:9080
    uv run python tools/esp32_en_pc.py 8081
    MQTT_CODIGO=prueba-pc uv run python tools/esp32_en_pc.py   # otro tema MQTT

Mientras corre, se pueden escribir comandos en la terminal:
    boton          simula presionar el botón «Avisar»
    temp 38        cambia la temperatura del DHT22
    hum 70         cambia la humedad
    pot 25         mueve el potenciómetro (0-100)
"""

import asyncio
import gc
import os
import queue
import re
import sys
import threading
import time
import types
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parent.parent / "wokwi" / "main.py"
PUERTO = int(sys.argv[1]) if len(sys.argv) > 1 else 9080

simulado = {"temperatura": 24.0, "humedad": 55.0, "pot": 50, "boton": None}


# ---------- time / gc / asyncio de MicroPython ----------
_arranque = time.monotonic()
time.ticks_ms = lambda: int((time.monotonic() - _arranque) * 1000)
time.ticks_diff = lambda a, b: a - b
time.sleep_ms = lambda ms: time.sleep(ms / 1000)
gc.mem_free = lambda: 98_304


async def _sleep_ms(ms):
    await asyncio.sleep(ms / 1000)


asyncio.sleep_ms = _sleep_ms
_start_server = asyncio.start_server


async def _start_server_local(callback, host, port, **kwargs):
    print(f"[pc] puerto {port} del ESP32 -> http://localhost:{PUERTO}")
    return await _start_server(callback, "127.0.0.1", PUERTO, **kwargs)


asyncio.start_server = _start_server_local


def modulo(nombre, **atributos):
    m = types.ModuleType(nombre)
    m.__dict__.update(atributos)
    sys.modules[nombre] = m


# ---------- machine ----------
class Pin:
    OUT, IN, PULL_UP, IRQ_FALLING = 1, 0, 2, 4

    def __init__(self, numero, modo=None, pull=None):
        self.numero, self._valor = numero, 0

    def value(self, v=None):
        if v is None:
            return self._valor
        self._valor = v

    def irq(self, trigger=None, handler=None):
        if self.numero == 4:
            simulado["boton"] = lambda: handler(self)


class PWM:
    def __init__(self, pin, freq=1000, duty_u16=0):
        self._freq, self._duty = freq, duty_u16

    def freq(self, f=None):
        if f is None:
            return self._freq
        self._freq = f

    def duty_u16(self, d=None):
        if d is None:
            return self._duty
        self._duty = d


class ADC:
    ATTN_11DB = 3

    def __init__(self, pin):
        pass

    def atten(self, valor):
        pass

    def read_u16(self):
        return int(simulado["pot"] * 65535 / 100)


class I2C:
    def __init__(self, *args, **kwargs):
        pass

    def writeto(self, direccion, datos):
        pass

    def writevto(self, direccion, partes):
        pass


modulo("machine", Pin=Pin, PWM=PWM, ADC=ADC, I2C=I2C, unique_id=lambda: b"\x24\x6f\x28\x00\x00\x01")


# ---------- umqtt.simple sobre paho-mqtt ----------
class MQTTClient:
    def __init__(self, client_id, server, port=0, keepalive=0, **kwargs):
        import paho.mqtt.client as mqtt

        self._mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id + "-pc")
        self._server = os.getenv("MQTT_BROKER", server)
        self._port = int(os.getenv("MQTT_PUERTO", port or 1883))
        self._keepalive = keepalive or 60
        self._entrantes = queue.Queue()
        self._callback = None
        self._mqtt.on_message = lambda c, u, m: self._entrantes.put((m.topic.encode(), m.payload))
        self.sock = self

    def set_callback(self, f):
        self._callback = f

    def set_last_will(self, topic, msg, retain=False, qos=0):
        self._mqtt.will_set(topic, msg, qos=qos, retain=retain)

    def connect(self):
        print(f"[pc] MQTT -> {self._server}:{self._port}")
        self._mqtt.connect(self._server, self._port, self._keepalive)
        self._mqtt.loop_start()
        # umqtt.simple.connect() no vuelve hasta recibir el CONNACK.
        limite = time.monotonic() + 5
        while not self._mqtt.is_connected():
            if time.monotonic() > limite:
                self.close()
                raise OSError("el broker no respondió")
            time.sleep(0.02)

    def subscribe(self, topic, qos=0):
        self._mqtt.subscribe(topic, qos)

    def publish(self, topic, msg, retain=False, qos=0):
        if not self._mqtt.is_connected():
            raise OSError("MQTT desconectado")
        self._mqtt.publish(topic, msg, qos=qos, retain=retain)

    def check_msg(self):
        while not self._entrantes.empty():
            self._callback(*self._entrantes.get())

    def close(self):
        self._mqtt.loop_stop()
        self._mqtt.disconnect()


modulo("umqtt")
modulo("umqtt.simple", MQTTClient=MQTTClient)


# ---------- dht / neopixel / framebuf / network ----------
class DHT22:
    def __init__(self, pin):
        pass

    def measure(self):
        pass

    def temperature(self):
        return simulado["temperatura"]

    def humidity(self):
        return simulado["humedad"]


modulo("dht", DHT22=DHT22)


class NeoPixel:
    def __init__(self, pin, n):
        self.pixeles = [(0, 0, 0)] * n

    def __setitem__(self, i, color):
        self.pixeles[i] = color

    def write(self):
        pass


modulo("neopixel", NeoPixel=NeoPixel)


class FrameBuffer:
    def __init__(self, buffer, ancho, alto, formato):
        pass

    def fill(self, c): pass
    def text(self, s, x, y, c=1): pass
    def rect(self, x, y, w, h, c): pass
    def fill_rect(self, x, y, w, h, c): pass


modulo("framebuf", FrameBuffer=FrameBuffer, MONO_VLSB=0)


class WLAN:
    def __init__(self, interfaz):
        pass

    def active(self, valor=None):
        return True

    def connect(self, ssid, clave):
        pass

    def isconnected(self):
        return True

    def ifconfig(self):
        return ("10.13.37.2", "255.255.255.0", "10.13.37.1", "8.8.8.8")

    def status(self, clave=None):
        return -58 if clave == "rssi" else 1010


modulo("network", WLAN=WLAN, STA_IF=0)


# ---------- comandos por terminal ----------
def leer_comandos():
    for linea in sys.stdin:
        partes = linea.split()
        if not partes:
            continue
        try:
            if partes[0] == "boton" and simulado["boton"]:
                simulado["boton"]()
            elif partes[0] == "temp":
                simulado["temperatura"] = float(partes[1])
            elif partes[0] == "hum":
                simulado["humedad"] = float(partes[1])
            elif partes[0] == "pot":
                simulado["pot"] = max(0, min(100, int(partes[1])))
            else:
                print("[pc] comandos: boton | temp N | hum N | pot N")
        except (IndexError, ValueError):
            print("[pc] valor inválido")


threading.Thread(target=leer_comandos, daemon=True).start()

fuente = FIRMWARE.read_text(encoding="utf-8")
if os.getenv("MQTT_CODIGO"):
    # Otro código para no chocar con el ESP32 de Wokwi en el mismo broker.
    fuente = re.sub(r'^CODIGO = ".*?"', f'CODIGO = "{os.environ["MQTT_CODIGO"]}"', fuente, count=1, flags=re.M)
codigo = compile(fuente, str(FIRMWARE), "exec")
exec(codigo, {"__name__": "__main__"})
