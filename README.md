# Panel IoT UNAH · Flet + ESP32 en Wokwi

Interfaz en **Flet** que controla un **ESP32 simulado en Wokwi** (MicroPython).
El ESP32 habla con la app de dos formas, con el mismo JSON:

```
MQTT (funciona con Wokwi gratis)
  ESP32 (Wokwi) ──▶ broker MQTT ◀── App Flet
                    .../estado   (el ESP32 publica cada segundo)
                    .../control  (la app manda comandos)

HTTP directo (el ESP32 es el servidor web)
  App Flet ── GET /api/estado, POST /api/control ──▶ ESP32 :80
```

Wokwi gratis sólo permite conexiones **de salida** desde el ESP32. Por eso
MQTT funciona siempre (el ESP32 y la app se conectan al broker), mientras que
HTTP directo necesita el *Private IoT Gateway* de Wokwi, que es de pago.

## Qué hace

| En la app de Flet | En el ESP32 |
| --- | --- |
| Campo **Nombre** | Saludo en la pantalla OLED |
| Casilla **Acepto los términos** | Habilita los controles |
| Botón **Encender LED** | LED rojo, GPIO 26 |
| **Slider** de nivel | Ángulo del servo (GPIO 18) y LEDs encendidos del anillo NeoPixel (GPIO 13) |
| **Color favorito** | LED RGB (GPIO 25/33/32) y color del anillo |
| **Efecto del anillo** | Nivel, Respirar, Arcoíris o Termómetro (el anillo sigue la temperatura) |
| **Framework favorito** | Se muestra en la OLED |
| **Notificaciones** | Pitidos del zumbador (GPIO 27) y avisos en la app |
| **Mensaje para la OLED** | Texto en la OLED; los mensajes largos se desplazan |
| **Tocar melodía** | Melodía en el zumbador |

Lo que la app recibe del ESP32 y muestra: temperatura y humedad (DHT22),
potenciómetro, pulsaciones del botón **Avisar**, señal WiFi, tiempo activo,
memoria libre, una copia en vivo del anillo NeoPixel y la bitácora de eventos
en una caja de texto. Por encima de 35 °C el ESP32 emite una alerta.

## Cómo ejecutarlo con Wokwi (MQTT)

1. En [wokwi.com](https://wokwi.com/projects/new/micropython-esp32), en un
   proyecto ESP32 MicroPython, pega `wokwi/main.py` en `main.py` y
   `wokwi/diagram.json` en `diagram.json`.
2. Inicia la simulación. El monitor serial muestra
   `MQTT listo, tema: unah/paradigmas/unah-esp32-4821`.
3. Abre la app:

   ```bash
   uv run flet run            # escritorio
   uv run flet run --web      # navegador
   ```

   Deja la conexión en **MQTT**, con el broker `broker.hivemq.com:1883` y el
   código `unah-esp32-4821`. El código debe ser igual a `CODIGO` en
   `wokwi/main.py`; cámbialo por uno propio para que nadie más use tu tema
   en el broker público.

### Con tu propio broker (Mosquitto + ngrok)

```bash
brew install mosquitto
/opt/homebrew/opt/mosquitto/sbin/mosquitto -c tools/mosquitto.conf
ngrok tcp 1883      # muestra algo como tcp://4.tcp.ngrok.io:15432
```

- En `wokwi/main.py`: `MQTT_BROKER = "4.tcp.ngrok.io"` y `MQTT_PUERTO = 15432`
  (lo que muestre ngrok; cambia cada vez que reinicias ngrok).
- En la app: broker `localhost:1883` (la app está en la misma computadora).

ngrok pide registrar una tarjeta para los túneles TCP en el plan gratuito (no
la cobra). `tools/mosquitto.conf` no tiene contraseña: úsalo sólo para la demo.

## HTTP directo (el ESP32 como servidor web)

| Ruta | Uso |
| --- | --- |
| `GET /` | Página web pequeña servida por el ESP32 |
| `GET /api/estado?desde=N` | Sensores, salidas, colores del anillo y eventos con id mayor que `N` |
| `POST /api/control?desde=N` | JSON con los cambios; responde el estado nuevo |

En Wokwi necesita el [Wokwi IoT Gateway](https://github.com/wokwi/wokwigw/releases/latest)
(`wokwigw`) y, en el editor, **F1 → Enable Private Wokwi IoT Gateway** (plan de
pago). Entonces el ESP32 responde en `http://localhost:9080`: en la app elige
**HTTP directo** con esa dirección.

## Comandos

Por MQTT (tema `.../control`) o por HTTP (`POST /api/control`), todos opcionales:

```json
{"led": true, "nivel": 75, "color": "Rojo", "rgb": [255, 0, 0],
 "efecto": "arcoiris", "nombre": "Ana", "framework": "Flet",
 "notificaciones": true, "mensaje": "Hola", "accion": "melodia"}
```

`efecto`: `fijo`, `respirar`, `arcoiris` o `termometro`.
`accion`: `melodia`, `pitido` o `reiniciar_contador`.

## Probar sin Wokwi

`tools/esp32_en_pc.py` ejecuta el mismo `wokwi/main.py` en la computadora, con
imitaciones de los módulos de MicroPython: servidor HTTP en
`http://localhost:9080` y MQTT con el broker del firmware.

```bash
uv run python tools/esp32_en_pc.py
MQTT_CODIGO=prueba-pc MQTT_BROKER=localhost uv run python tools/esp32_en_pc.py
```

En esa terminal puedes escribir `boton`, `temp 38`, `hum 70` o `pot 25` para
simular el hardware.

La app también lee estas variables al arrancar: `ESP32_MODO` (`mqtt` o
`http`), `MQTT_BROKER`, `MQTT_CODIGO` y `ESP32_URL`.

## Pruebas

```bash
uv run pytest
```
