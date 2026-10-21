# ESP32 (MicroPython en Wokwi) para el Panel IoT UNAH.
#
# El ESP32 habla con la app de Flet de dos formas, con el mismo JSON:
#
# 1) MQTT (funciona en Wokwi gratis): el ESP32 se conecta a un broker y
#      publica  unah/paradigmas/<CODIGO>/estado    (cada segundo, retenido)
#      escucha  unah/paradigmas/<CODIGO>/control   (comandos de la app)
#      publica  unah/paradigmas/<CODIGO>/conexion  ("online" / "offline")
#
# 2) Servidor web propio en el puerto 80 (necesita el Private IoT Gateway
#    de Wokwi para alcanzarlo desde la computadora):
#      GET  /                      panel web pequeño servido por el ESP32
#      GET  /api/estado?desde=N    sensores, salidas y eventos con id > N
#      POST /api/control?desde=N   JSON con los cambios; responde el estado
#
# Comandos que acepta /control (todos opcionales):
#   {"led": true, "nivel": 0-100, "color": "Rojo", "rgb": [255, 0, 0],
#    "efecto": "fijo|respirar|arcoiris|termometro", "nombre": "Ana",
#    "framework": "Flet", "notificaciones": true, "mensaje": "Hola",
#    "accion": "melodia|pitido|reiniciar_contador"}
#
# Importar librerias
import asyncio
import binascii
import gc
import json
import math
import time

import dht
import framebuf
import network
import neopixel
from machine import ADC, I2C, PWM, Pin, unique_id
from umqtt.simple import MQTTClient

# ---------- MQTT: cambia esto segun tu broker ----------
# Broker publico (no necesita nada) o tu propio Mosquitto publicado con
# `ngrok tcp 1883`: ahi pon el host y el puerto que muestra ngrok,
# por ejemplo MQTT_BROKER = "4.tcp.ngrok.io" y MQTT_PUERTO = 15432.
MQTT_BROKER = "broker.hivemq.com"
MQTT_PUERTO = 1883
CODIGO = "unah-esp32-4821"      # debe ser el mismo que en la app de Flet
TEMA = "unah/paradigmas/" + CODIGO

PUERTO = 80
NUM_PIXELES = 16
TEMP_ALERTA = 35        # grados C; por encima se emite una alerta
EFECTOS = ("fijo", "respirar", "arcoiris", "termometro")
FRAMEWORKS = ("Flet", "Tkinter", "PyQt6", "PySide6")


# ---------- Pantalla OLED SSD1306 (driver minimo, sin archivos extra) ----------
class SSD1306_I2C(framebuf.FrameBuffer):
    def __init__(self, ancho, alto, i2c, direccion=0x3C):
        self.ancho = ancho
        self.paginas = alto // 8
        self.i2c = i2c
        self.direccion = direccion
        self.buffer = bytearray(self.paginas * ancho)
        super().__init__(self.buffer, ancho, alto, framebuf.MONO_VLSB)
        for c in (0xAE, 0x20, 0x00, 0x40, 0xA1, 0xA8, alto - 1, 0xC8, 0xD3, 0x00,
                  0xDA, 0x12, 0xD5, 0x80, 0xD9, 0xF1, 0xDB, 0x30, 0x81, 0xFF,
                  0xA4, 0xA6, 0x8D, 0x14, 0xAF):
            self.comando(c)

    def comando(self, c):
        self.i2c.writeto(self.direccion, bytes((0x80, c)))

    def show(self):
        for c in (0x21, 0, self.ancho - 1, 0x22, 0, self.paginas - 1):
            self.comando(c)
        self.i2c.writevto(self.direccion, (b"\x40", self.buffer))


# ---------- Hardware ----------
led = Pin(26, Pin.OUT)
rgb = [PWM(Pin(p), freq=1000, duty_u16=0) for p in (25, 33, 32)]  # R, G, B
servo = PWM(Pin(18), freq=50)
zumbador = PWM(Pin(27), freq=2000, duty_u16=0)
anillo = neopixel.NeoPixel(Pin(13), NUM_PIXELES)
sensor = dht.DHT22(Pin(15))
pot = ADC(Pin(34))
pot.atten(ADC.ATTN_11DB)
boton = Pin(4, Pin.IN, Pin.PULL_UP)
oled = SSD1306_I2C(128, 64, I2C(0, scl=Pin(22), sda=Pin(21)))

# ---------- Estado ----------
salidas = {
    "led": False,
    "nivel": 50,
    "color": "Azul UNAH",
    "rgb": [0, 60, 255],
    "efecto": "fijo",
    "nombre": "",
    "framework": "Flet",
    "notificaciones": True,
    "mensaje": "",
}
lecturas = {"temperatura": None, "humedad": None}
eventos = []            # [(id, segundos, texto)]
ultimo_id = 0
pulsaciones = 0
ultimo_toque = 0
ultima_peticion = 0     # ticks_ms de la ultima peticion HTTP atendida
peticiones = 0          # comandos atendidos (HTTP + MQTT)
mqtt_conectado = False
publicar_ya = False     # pide a la tarea MQTT publicar el estado de inmediato
sonando = False
colores_anillo = [(0, 0, 0)] * NUM_PIXELES
inicio = time.ticks_ms()
wlan = network.WLAN(network.STA_IF)
ip = "0.0.0.0"


def segundos_activo():
    return time.ticks_diff(time.ticks_ms(), inicio) // 1000


def registrar(texto, tipo="comando"):
    """tipo: "comando" (lo pidio la interfaz), "aviso" (hardware) o "sistema"."""
    global ultimo_id
    ultimo_id += 1
    eventos.append((ultimo_id, segundos_activo(), tipo, texto))
    if len(eventos) > 30:
        eventos.pop(0)
    print("[evento]", texto)


def al_presionar(pin):
    global pulsaciones, ultimo_toque
    ahora = time.ticks_ms()
    if time.ticks_diff(ahora, ultimo_toque) > 250:  # antirrebote
        ultimo_toque = ahora
        pulsaciones += 1


boton.irq(trigger=Pin.IRQ_FALLING, handler=al_presionar)


# ---------- Sonido ----------
MELODIA = ((523, 120), (659, 120), (784, 120), (1047, 260), (0, 80), (784, 120), (1047, 360))


async def tocar(notas):
    global sonando
    if sonando:
        return
    sonando = True
    try:
        for frecuencia, duracion in notas:
            if frecuencia:
                zumbador.freq(frecuencia)
                zumbador.duty_u16(20000)
            await asyncio.sleep_ms(duracion)
            zumbador.duty_u16(0)
            await asyncio.sleep_ms(25)
    finally:
        zumbador.duty_u16(0)
        sonando = False


def pitar(veces=1):
    if salidas["notificaciones"]:
        asyncio.create_task(tocar(((2093, 70), (0, 60)) * veces))


# ---------- Salidas ----------
def mover_servo(nivel):
    angulo = nivel * 180 // 100
    # 0.5 ms = 0 grados, 2.5 ms = 180 grados (periodo de 20 ms)
    servo.duty_u16(int((0.5 + angulo / 90) / 20 * 65535))
    return angulo


def poner_rgb(color):
    for canal, valor in zip(rgb, color):
        canal.duty_u16(valor * 257)


def rueda(posicion):
    """Color del arcoiris para una posicion 0-255."""
    posicion %= 256
    if posicion < 85:
        return (255 - posicion * 3, posicion * 3, 0)
    if posicion < 170:
        posicion -= 85
        return (0, 255 - posicion * 3, posicion * 3)
    posicion -= 170
    return (posicion * 3, 0, 255 - posicion * 3)


def escalar(color, factor):
    return tuple(int(c * factor) for c in color)


def a_entero(valor, minimo, maximo):
    return max(minimo, min(maximo, int(valor)))


def aplicar(cambios):
    """Valida y aplica los comandos recibidos. Devuelve la lista de errores."""
    global pulsaciones
    errores = []
    if "led" in cambios:
        nuevo = bool(cambios["led"])
        if nuevo != salidas["led"]:
            salidas["led"] = nuevo
            registrar("LED " + ("encendido" if nuevo else "apagado"))
            pitar()
    if "nivel" in cambios:
        try:
            salidas["nivel"] = a_entero(cambios["nivel"], 0, 100)
        except (TypeError, ValueError):
            errores.append("nivel debe ser un numero 0-100")
    if "rgb" in cambios:
        try:
            color = [a_entero(c, 0, 255) for c in cambios["rgb"]][:3]
            if len(color) != 3:
                raise ValueError
            nombre = str(cambios.get("color") or "personalizado")[:20]
            if color != salidas["rgb"]:
                registrar("Color: " + nombre)
            salidas["rgb"], salidas["color"] = color, nombre
        except (TypeError, ValueError):
            errores.append("rgb debe ser [r, g, b]")
    if "efecto" in cambios:
        if cambios["efecto"] in EFECTOS:
            if cambios["efecto"] != salidas["efecto"]:
                salidas["efecto"] = cambios["efecto"]
                registrar("Efecto del anillo: " + salidas["efecto"])
        else:
            errores.append("efecto desconocido")
    if "nombre" in cambios:
        nombre = str(cambios["nombre"]).strip()[:16]
        if nombre and nombre != salidas["nombre"]:
            registrar("Hola, %s!" % nombre)
        salidas["nombre"] = nombre
    if "framework" in cambios:
        if cambios["framework"] in FRAMEWORKS:
            if cambios["framework"] != salidas["framework"]:
                salidas["framework"] = cambios["framework"]
                registrar("Framework elegido: " + salidas["framework"])
        else:
            errores.append("framework desconocido")
    if "notificaciones" in cambios:
        nuevo = bool(cambios["notificaciones"])
        if nuevo != salidas["notificaciones"]:
            salidas["notificaciones"] = nuevo
            registrar("Notificaciones " + ("activadas" if nuevo else "desactivadas"))
    if "mensaje" in cambios:
        salidas["mensaje"] = str(cambios["mensaje"]).strip()[:32]
        if salidas["mensaje"]:
            registrar("Mensaje en OLED: " + salidas["mensaje"])
            pitar()
    accion = cambios.get("accion")
    if accion == "melodia":
        registrar("Tocando melodia")
        asyncio.create_task(tocar(MELODIA))
    elif accion == "pitido":
        asyncio.create_task(tocar(((1568, 150),)))
    elif accion == "reiniciar_contador":
        pulsaciones = 0
        registrar("Contador del boton reiniciado")
    elif accion is not None:
        errores.append("accion desconocida")

    led.value(1 if salidas["led"] else 0)
    mover_servo(salidas["nivel"])
    return errores


def estado(desde=0):
    try:
        rssi = wlan.status("rssi")
    except Exception:
        rssi = None
    return {
        "dispositivo": "ESP32 UNAH",
        "ip": ip,
        "tiempo_activo": segundos_activo(),
        "memoria_libre": gc.mem_free(),
        "peticiones": peticiones,
        "mqtt": mqtt_conectado,
        "rssi": rssi,
        "sensores": {
            "temperatura": lecturas["temperatura"],
            "humedad": lecturas["humedad"],
            "potenciometro": (pot.read_u16() * 100 + 32767) // 65535,
            "pulsaciones": pulsaciones,
        },
        "salidas": dict(salidas),
        "servo_grados": salidas["nivel"] * 180 // 100,
        "anillo": ["#%02x%02x%02x" % c for c in colores_anillo],
        "eventos": [{"id": i, "t": t, "tipo": k, "texto": x}
                    for i, t, k, x in eventos if i > desde],
        "ultimo_evento": ultimo_id,
    }


# ---------- Tareas en segundo plano ----------
async def tarea_sensores():
    alerta = False
    vistas = 0
    while True:
        try:
            sensor.measure()
            lecturas["temperatura"] = sensor.temperature()
            lecturas["humedad"] = sensor.humidity()
        except OSError:
            lecturas["temperatura"] = lecturas["humedad"] = None
        t = lecturas["temperatura"]
        if t is not None and t > TEMP_ALERTA and not alerta:
            alerta = True
            registrar("Alerta: temperatura alta (%.1f C)" % t, "aviso")
            pitar(3)
        elif t is not None and t < TEMP_ALERTA - 1 and alerta:
            alerta = False
            registrar("Temperatura normal (%.1f C)" % t, "aviso")
        # Revisar el boton varias veces entre lecturas del DHT22 (cada 2 s)
        for _ in range(10):
            if pulsaciones != vistas:
                vistas = pulsaciones
                if vistas:
                    registrar("Boton presionado (#%d)" % vistas, "aviso")
                    pitar()
            await asyncio.sleep_ms(200)


async def tarea_anillo():
    global colores_anillo
    paso = 0
    while True:
        efecto = salidas["efecto"]
        color = tuple(salidas["rgb"])
        if efecto == "respirar":
            factor = 0.15 + 0.85 * (math.sin(paso / 6) + 1) / 2
            colores = [escalar(color, factor)] * NUM_PIXELES
            poner_rgb(colores[0])
        elif efecto == "arcoiris":
            colores = [rueda(paso * 8 + i * 256 // NUM_PIXELES) for i in range(NUM_PIXELES)]
            poner_rgb(colores[0])
        elif efecto == "termometro":
            t = lecturas["temperatura"]
            # -10 C (azul) ... 50 C (rojo)
            fraccion = 0 if t is None else max(0, min(1, (t + 10) / 60))
            tono = (int(fraccion * 255), 0, int((1 - fraccion) * 255))
            encendidos = round(fraccion * NUM_PIXELES)
            colores = [tono if i < encendidos else (0, 0, 0) for i in range(NUM_PIXELES)]
            poner_rgb(tono)
        else:  # fijo: el anillo muestra el nivel del slider
            encendidos = round(salidas["nivel"] * NUM_PIXELES / 100)
            colores = [color if i < encendidos else (0, 0, 0) for i in range(NUM_PIXELES)]
            poner_rgb(color)
        for i, c in enumerate(colores):
            anillo[i] = c
        anillo.write()
        colores_anillo = colores
        paso += 1
        await asyncio.sleep_ms(80)


async def tarea_pantalla():
    while True:
        cliente = ultima_peticion and time.ticks_diff(time.ticks_ms(), ultima_peticion) < 5000
        oled.fill(0)
        oled.fill_rect(0, 0, 128, 11, 1)
        oled.text("UNAH ESP32", 2, 2, 0)
        if cliente:
            oled.text("WEB", 102, 2, 0)
        else:
            oled.text("MQTT" if mqtt_conectado else "----", 94, 2, 0)
        oled.text(("Hola " + (salidas["nombre"] or "invitado"))[:16], 0, 14)
        oled.text(("UI: " + salidas["framework"])[:16], 0, 24)
        linea = "LED:" + ("ON " if salidas["led"] else "OFF")
        if lecturas["temperatura"] is not None:
            linea += " %dC %d%%" % (int(lecturas["temperatura"]), int(lecturas["humedad"]))
        oled.text(linea[:16], 0, 34)
        mensaje = salidas["mensaje"] or (ip + ":" + str(PUERTO))
        if len(mensaje) > 16:  # desplazar mensajes largos
            desfase = (time.ticks_ms() // 400) % (len(mensaje) + 3)
            mensaje = (mensaje + "   " + mensaje)[desfase:desfase + 16]
        oled.text(mensaje, 0, 44)
        oled.text("%3d%%" % salidas["nivel"], 0, 55)
        oled.rect(36, 55, 92, 9, 1)
        oled.fill_rect(38, 57, 88 * salidas["nivel"] // 100, 5, 1)
        oled.show()
        await asyncio.sleep_ms(250)


# ---------- Servidor HTTP ----------
PAGINA = """<!doctype html><html lang=es><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ESP32 UNAH</title><style>
body{font-family:system-ui,sans-serif;background:#0B2E6B;color:#fff;margin:0;padding:24px}
.c{max-width:420px;margin:auto;background:#fff;color:#0B2E6B;border-radius:18px;padding:22px}
h1{margin:0 0 4px;font-size:22px}p{margin:0 0 16px;color:#5B6478}
button{background:#1C4E9C;color:#fff;border:0;border-radius:10px;padding:12px 16px;font-size:15px;cursor:pointer}
input{width:100%}pre{background:#EEF2F8;border-radius:10px;padding:12px;font-size:12px;white-space:pre-wrap}
</style><div class=c><h1>ESP32 UNAH</h1><p>Panel servido por el propio ESP32</p>
<button id=led onclick="ctl({led:!s.salidas.led})">LED</button>
<button onclick="ctl({accion:'melodia'})">Melodia</button>
<p style="margin:18px 0 4px">Nivel: <b id=num></b>%</p>
<input type=range min=0 max=100 id=nivel onchange="ctl({nivel:+this.value})">
<pre id=datos>Cargando...</pre></div><script>
let s={salidas:{}};const $=i=>document.getElementById(i);
function ver(j){s=j;$('led').textContent=j.salidas.led?'Apagar LED':'Encender LED';
$('num').textContent=j.salidas.nivel;$('nivel').value=j.salidas.nivel;
$('datos').textContent=JSON.stringify(j.sensores,null,1)}
function ctl(c){fetch('/api/control',{method:'POST',body:JSON.stringify(c)}).then(r=>r.json()).then(ver)}
function leer(){fetch('/api/estado?desde=999999').then(r=>r.json()).then(ver).catch(()=>0)}
leer();setInterval(leer,1500)</script></html>"""


def parametros(consulta):
    datos = {}
    for par in consulta.split("&"):
        clave, _, valor = par.partition("=")
        if clave:
            datos[clave] = valor
    return datos


async def leer_peticion(lector):
    linea = await lector.readline()
    if not linea:
        return None
    metodo, ruta, _ = linea.decode().split(" ", 2)
    largo = 0
    while True:
        cabecera = await lector.readline()
        if not cabecera or cabecera == b"\r\n":
            break
        nombre, _, valor = cabecera.decode().partition(":")
        if nombre.strip().lower() == "content-length":
            largo = min(int(valor.strip()), 2048)
    cuerpo = b""
    while len(cuerpo) < largo:
        trozo = await lector.read(largo - len(cuerpo))
        if not trozo:
            break
        cuerpo += trozo
    return metodo, ruta, cuerpo


def responder(metodo, ruta, cuerpo):
    ruta, _, consulta = ruta.partition("?")
    try:
        desde = int(parametros(consulta).get("desde", "0"))
    except ValueError:
        desde = 0
    if metodo == "OPTIONS":
        return "204 No Content", "text/plain", ""
    if metodo == "GET" and ruta == "/":
        return "200 OK", "text/html; charset=utf-8", PAGINA
    if metodo == "GET" and ruta == "/api/estado":
        return "200 OK", "application/json", json.dumps(estado(desde))
    if metodo == "POST" and ruta == "/api/control":
        try:
            cambios = json.loads((cuerpo or b"{}").decode())
            if not isinstance(cambios, dict):
                raise ValueError
        except Exception:
            return "400 Bad Request", "application/json", '{"error": "JSON invalido"}'
        errores = aplicar(cambios)
        respuesta = estado(desde)
        if errores:
            respuesta["errores"] = errores
        return "200 OK", "application/json", json.dumps(respuesta)
    return "404 Not Found", "application/json", '{"error": "ruta no encontrada"}'


async def atender(lector, escritor):
    global ultima_peticion, peticiones
    try:
        peticion = await asyncio.wait_for(leer_peticion(lector), 5)
        if peticion is None:
            return
        estado_http, tipo, contenido = responder(*peticion)
        datos = contenido.encode()
        escritor.write((
            "HTTP/1.1 %s\r\n"
            "Content-Type: %s\r\n"
            "Content-Length: %d\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n" % (estado_http, tipo, len(datos))
        ).encode())
        escritor.write(datos)
        await escritor.drain()
        ultima_peticion = time.ticks_ms()
        peticiones += 1
    except Exception as error:
        print("Error HTTP:", repr(error))
    finally:
        escritor.close()
        await escritor.wait_closed()
        gc.collect()


# ---------- MQTT ----------
def al_recibir(tema, mensaje):
    global publicar_ya, peticiones
    try:
        cambios = json.loads(mensaje.decode())
        if not isinstance(cambios, dict):
            raise ValueError
    except Exception:
        print("MQTT: comando invalido", mensaje)
        return
    peticiones += 1
    errores = aplicar(cambios)
    if errores:
        registrar("Comando con errores: " + ", ".join(errores), "sistema")
    publicar_ya = True


async def tarea_mqtt():
    global mqtt_conectado, publicar_ya
    id_cliente = "esp32-" + binascii.hexlify(unique_id()).decode()
    while True:
        cliente = None
        try:
            print("Conectando a MQTT %s:%d" % (MQTT_BROKER, MQTT_PUERTO))
            # keepalive corto: si la simulacion se detiene, el broker publica
            # "offline" (ultima voluntad) en ~20 s. Se publica cada segundo,
            # asi que nunca se llega al limite estando conectado.
            cliente = MQTTClient(id_cliente, MQTT_BROKER, port=MQTT_PUERTO, keepalive=15)
            cliente.set_callback(al_recibir)
            cliente.set_last_will(TEMA + "/conexion", b"offline", retain=True)
            cliente.connect()
            cliente.subscribe(TEMA + "/control")
            cliente.publish(TEMA + "/conexion", b"online", retain=True)
            mqtt_conectado = True
            registrar("MQTT conectado a " + MQTT_BROKER, "sistema")
            print("MQTT listo, tema:", TEMA)
            ultimo_envio = time.ticks_ms() - 1000
            while True:
                cliente.check_msg()
                ahora = time.ticks_ms()
                # Publicar cada segundo tambien mantiene viva la conexion.
                if publicar_ya or time.ticks_diff(ahora, ultimo_envio) >= 1000:
                    publicar_ya = False
                    ultimo_envio = ahora
                    datos = json.dumps(estado(ultimo_id - 10))
                    cliente.publish(TEMA + "/estado", datos.encode(), retain=True)
                await asyncio.sleep_ms(50)
        except Exception as error:
            if mqtt_conectado:
                registrar("MQTT desconectado, reintentando", "sistema")
            mqtt_conectado = False
            print("Error MQTT:", repr(error))
            try:
                cliente.sock.close()
            except Exception:
                pass
            await asyncio.sleep(3)


# ---------- Arranque ----------
def conectar_wifi():
    global ip
    print("Conectando a la Red Wifi")
    oled.fill(0)
    oled.text("Conectando WiFi", 0, 28)
    oled.show()
    wlan.active(True)
    wlan.connect("Wokwi-GUEST", "")
    while not wlan.isconnected():
        print("*", end="")
        time.sleep(0.5)
    ip = wlan.ifconfig()[0]
    print("\nConectado a la WIFI, IP:", ip)
    registrar("WiFi conectado, IP " + ip, "sistema")


async def principal():
    aplicar({})
    conectar_wifi()
    await asyncio.start_server(atender, "0.0.0.0", PUERTO)
    print("Servidor web listo en http://%s:%d" % (ip, PUERTO))
    print("Desde la computadora (Private Gateway): http://localhost:9080")
    registrar("Servidor web listo en el puerto %d" % PUERTO, "sistema")
    asyncio.create_task(tarea_sensores())
    asyncio.create_task(tarea_anillo())
    asyncio.create_task(tarea_mqtt())
    asyncio.create_task(tocar(((1047, 90), (1568, 140))))
    await tarea_pantalla()


asyncio.run(principal())
