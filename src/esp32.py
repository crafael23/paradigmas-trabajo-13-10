"""Conexión entre la app de Flet y el ESP32 (Wokwi), por MQTT o por HTTP.

Las dos formas usan el mismo JSON (ver `wokwi/main.py`):

* MQTT: el ESP32 y la app se conectan a un broker. El ESP32 publica su
  estado en `unah/paradigmas/<codigo>/estado` y escucha comandos en
  `.../control`. Funciona con Wokwi gratis, porque los dos sólo hacen
  conexiones de salida.
* HTTP: la app le habla directo al servidor web del ESP32
  (`GET /api/estado`, `POST /api/control`). En Wokwi necesita el Private
  IoT Gateway (localhost:9080 -> puerto 80 del ESP32).

Los cambios de la interfaz no se envían uno por uno: se acumulan en
`pendientes` y el ciclo de sincronización los manda juntos en la siguiente
vuelta. Así un slider que se arrastra no satura al ESP32. Flet ejecuta los
eventos síncronos en otros hilos, por eso `pendientes` se protege con un lock.
"""

import json
import threading
import time

import httpx
import paho.mqtt.client as mqtt

MQTT_BROKER_POR_DEFECTO = "broker.hivemq.com:1883"
CODIGO_POR_DEFECTO = "unah-esp32-4821"
# En Wokwi, el Private IoT Gateway reenvía localhost:9080 al puerto 80 del ESP32.
URL_POR_DEFECTO = "http://localhost:9080"

# Nombre visible -> (hex para la interfaz, RGB para el ESP32)
COLORES = {
    "Azul UNAH": ("#1C4E9C", (0, 60, 255)),
    "Oro UNAH": ("#F2B705", (255, 160, 0)),
    "Rojo": ("#E53935", (255, 0, 0)),
    "Verde": ("#43A047", (0, 255, 0)),
    "Morado": ("#8E24AA", (160, 0, 255)),
    "Blanco": ("#F5F5F5", (255, 255, 255)),
}

FRAMEWORKS = ["Flet", "Tkinter", "PyQt6", "PySide6"]

# Valor que entiende el ESP32 -> texto para la interfaz
EFECTOS = {
    "fijo": "Nivel",
    "respirar": "Respirar",
    "arcoiris": "Arcoíris",
    "termometro": "Termómetro",
}

# Tras editar un control, se ignora lo que diga el ESP32 sobre él durante
# este tiempo, para que una respuesta vieja no "regrese" el control.
VENTANA_EDICION = 2.0
# Por MQTT el ESP32 publica cada segundo; si pasa esto sin noticias, se
# considera desconectado.
MQTT_SIN_NOTICIAS = 5.0


class SinConexion(Exception):
    """El ESP32 no respondió; el mensaje explica por qué."""


def normalizar_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def separar_broker(texto: str) -> tuple[str, int]:
    """'host:puerto' -> (host, puerto). Sin puerto se usa 1883."""
    texto = texto.strip().removeprefix("mqtt://").rstrip("/")
    host, _, puerto = texto.rpartition(":")
    if not host or not puerto.isdigit():
        return texto, 1883
    return host, int(puerto)


class Conexion:
    """Lo común a las dos formas de hablar con el ESP32."""

    descripcion = ""
    nombre = ""

    def __init__(self):
        self.ultimo_evento = 0
        self.latencia_ms: float | None = None   # ida y vuelta del último comando o consulta
        self._pendientes: dict = {}
        self._editado: dict[str, float] = {}
        self._lock = threading.Lock()

    def pedir(self, **cambios) -> None:
        """Encola cambios para el ESP32; se envían en la siguiente vuelta."""
        ahora = time.monotonic()
        with self._lock:
            self._pendientes.update(cambios)
            for clave in cambios:
                self._editado[clave] = ahora

    def hay_pendientes(self) -> bool:
        with self._lock:
            return bool(self._pendientes)

    def recien_editado(self, clave: str) -> bool:
        with self._lock:
            if clave in self._pendientes:
                return True
            return time.monotonic() - self._editado.get(clave, 0) < VENTANA_EDICION

    def _tomar_pendientes(self) -> dict:
        with self._lock:
            cambios, self._pendientes = self._pendientes, {}
            return cambios

    def _devolver_pendientes(self, cambios: dict) -> None:
        """No llegaron al ESP32: se reintentan sin pisar cambios más nuevos.
        Las acciones (melodía...) no se repiten."""
        cambios.pop("accion", None)
        with self._lock:
            self._pendientes = {**cambios, **self._pendientes}

    def _filtrar_eventos(self, datos: dict) -> dict:
        """Deja sólo los eventos que la interfaz todavía no ha mostrado."""
        ultimo = datos.get("ultimo_evento", 0)
        if ultimo < self.ultimo_evento:
            # El ESP32 se reinició: sus ids volvieron a empezar.
            self.ultimo_evento = 0
            datos["reiniciado"] = True
        datos["eventos"] = [e for e in datos.get("eventos", []) if e["id"] > self.ultimo_evento]
        self.ultimo_evento = ultimo
        return datos

    async def sincronizar(self) -> dict:
        """Una vuelta: envía lo pendiente y devuelve el estado del ESP32."""
        raise NotImplementedError

    async def cerrar(self) -> None:
        pass


class ConexionHTTP(Conexion):
    """La app le pide los datos directo al servidor web del ESP32."""

    nombre = "HTTP"

    def __init__(self, url: str = URL_POR_DEFECTO, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        self.url = normalizar_url(url)
        self.descripcion = self.url
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(4.0), transport=transport)

    async def sincronizar(self) -> dict:
        parametros = {"desde": self.ultimo_evento}
        cambios = self._tomar_pendientes()
        inicio = time.monotonic()
        try:
            if cambios:
                try:
                    respuesta = await self._http.post(
                        f"{self.url}/api/control", params=parametros, json=cambios
                    )
                except httpx.TransportError:
                    self._devolver_pendientes(cambios)
                    raise
            else:
                respuesta = await self._http.get(f"{self.url}/api/estado", params=parametros)
            respuesta.raise_for_status()
            datos = respuesta.json()
        except httpx.HTTPStatusError as error:
            raise SinConexion(f"{self.url} respondió {error.response.status_code}") from error
        except httpx.TimeoutException as error:
            raise SinConexion(f"{self.url} no respondió a tiempo") from error
        except httpx.HTTPError as error:
            raise SinConexion(f"No se pudo conectar a {self.url}") from error
        except ValueError as error:
            raise SinConexion(f"{self.url} no respondió con JSON") from error
        self.latencia_ms = (time.monotonic() - inicio) * 1000
        return self._filtrar_eventos(datos)

    async def cerrar(self) -> None:
        await self._http.aclose()


class ConexionMQTT(Conexion):
    """La app y el ESP32 se encuentran en un broker MQTT."""

    nombre = "MQTT"

    def __init__(self, broker: str = MQTT_BROKER_POR_DEFECTO, codigo: str = CODIGO_POR_DEFECTO,
                 cliente: mqtt.Client | None = None):
        super().__init__()
        self.host, self.puerto = separar_broker(broker)
        self.codigo = codigo.strip()
        self.tema = f"unah/paradigmas/{self.codigo}"
        self.descripcion = f"mqtt://{self.host}:{self.puerto} · {self.tema}"
        self._estado: dict | None = None
        self._recibido = 0.0          # time.monotonic() del último estado
        self._esp32_en_linea: bool | None = None
        self._error: str | None = None
        self._enviado: float | None = None  # cuándo salió el último comando

        self._mqtt = cliente or mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._mqtt.on_connect = self._al_conectar
        self._mqtt.on_disconnect = self._al_desconectar
        self._mqtt.on_message = self._al_recibir
        if cliente is None:
            self._mqtt.reconnect_delay_set(1, 10)
            self._mqtt.connect_async(self.host, self.puerto, keepalive=30)
            self._mqtt.loop_start()   # hilo propio de paho; reconecta solo

    # --- callbacks de paho (corren en su hilo) ---
    def _al_conectar(self, cliente, userdata, flags, codigo, propiedades=None):
        if codigo.is_failure:
            self._error = f"El broker rechazó la conexión ({codigo})"
            return
        self._error = None
        cliente.subscribe([(self.tema + "/estado", 0), (self.tema + "/conexion", 0)])

    def _al_desconectar(self, cliente, userdata, flags, codigo, propiedades=None):
        self._error = f"Se perdió la conexión con el broker {self.host}:{self.puerto}"

    def _al_recibir(self, cliente, userdata, mensaje):
        if mensaje.topic.endswith("/conexion"):
            with self._lock:
                self._esp32_en_linea = mensaje.payload == b"online"
            return
        try:
            datos = json.loads(mensaje.payload)
        except ValueError:
            return
        ahora = time.monotonic()
        with self._lock:
            self._estado = datos
            # Un estado retenido puede ser viejo; sólo cuenta como "en línea"
            # si el ESP32 publicó después de que nos suscribimos.
            self._recibido = 0.0 if mensaje.retain else ahora
            # Tras un comando el ESP32 publica de inmediato: eso mide la ida y vuelta.
            if self._enviado is not None and not mensaje.retain:
                self.latencia_ms = (ahora - self._enviado) * 1000
                self._enviado = None

    # --- interfaz común ---
    async def sincronizar(self) -> dict:
        if not self._mqtt.is_connected():
            raise SinConexion(self._error or f"Conectando al broker {self.host}:{self.puerto}…")
        cambios = self._tomar_pendientes()
        if cambios:
            info = self._mqtt.publish(self.tema + "/control", json.dumps(cambios))
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                self._devolver_pendientes(cambios)
                raise SinConexion("No se pudo publicar el comando en el broker")
            with self._lock:
                self._enviado = time.monotonic()
        with self._lock:
            datos, recibido, en_linea = self._estado, self._recibido, self._esp32_en_linea
        if datos is None:
            raise SinConexion(f"Conectado al broker, esperando al ESP32 en «{self.tema}»")
        if en_linea is False or time.monotonic() - recibido > MQTT_SIN_NOTICIAS:
            raise SinConexion(f"El ESP32 no está publicando en «{self.tema}»")
        return self._filtrar_eventos(dict(datos))

    async def cerrar(self) -> None:
        self._mqtt.loop_stop()
        self._mqtt.disconnect()
