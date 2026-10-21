"""Panel IoT UNAH: interfaz en Flet para un ESP32 que es servidor web (Wokwi).

El ESP32 levanta su propio servidor HTTP; esta app es el cliente. Cada medio
segundo consulta `GET /api/estado` y, cuando el usuario cambia algo, envía
`POST /api/control`. Ver `esp32.py` y `wokwi/main.py`.

Ejecutar:
    uv run flet run            # app de escritorio
    uv run flet run --web      # en el navegador
"""

import asyncio
import math
import os
import time
from datetime import datetime, timedelta

import flet as ft

from esp32 import (
    CODIGO_POR_DEFECTO,
    COLORES,
    EFECTOS,
    FRAMEWORKS,
    MQTT_BROKER_POR_DEFECTO,
    URL_POR_DEFECTO,
    Conexion,
    ConexionHTTP,
    ConexionMQTT,
    SinConexion,
)

AZUL = "#0B2E6B"
AZUL_CLARO = "#1C4E9C"
ORO = "#F2B705"
VERDE = "#2E9E5B"
ROJO = "#D64545"
FONDO = "#EEF2F8"
TEXTO_SUAVE = "#5B6478"
APAGADO = "#26324D"

INTERVALO = 0.5          # segundos entre consultas al ESP32
MAX_LINEAS_BITACORA = 80


def tarjeta(titulo: str, icono: ft.IconData, contenido: list[ft.Control], **kwargs) -> ft.Container:
    return ft.Container(
        bgcolor=ft.Colors.WHITE,
        border_radius=20,
        padding=24,
        shadow=ft.BoxShadow(blur_radius=24, color="#1A0B2E6B", offset=ft.Offset(0, 8)),
        content=ft.Column(
            spacing=16,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    spacing=10,
                    controls=[
                        ft.Container(
                            bgcolor="#E8EEF9",
                            border_radius=10,
                            padding=8,
                            content=ft.Icon(icono, color=AZUL_CLARO, size=20),
                        ),
                        ft.Text(titulo, size=18, weight=ft.FontWeight.W_700, color=AZUL),
                    ],
                ),
                *contenido,
            ],
        ),
        **kwargs,
    )


def etiqueta(texto: str) -> ft.Text:
    return ft.Text(texto, size=12, weight=ft.FontWeight.W_600, color=TEXTO_SUAVE)


class Indicador(ft.Container):
    """Tarjeta pequeña con una lectura del ESP32."""

    def __init__(self, titulo: str, icono: ft.IconData, color: str, unidad: str = "", barra=False):
        self.unidad = unidad
        self.valor = ft.Text("—", size=22, weight=ft.FontWeight.W_700, color=AZUL)
        self.barra = ft.ProgressBar(value=0, color=color, bgcolor="#E3E8F2", border_radius=4, visible=barra)
        super().__init__(
            col={"xs": 6, "md": 4},
            bgcolor="#F6F8FC",
            border_radius=16,
            padding=14,
            border=ft.Border.all(1, "#E3E8F2"),
            content=ft.Column(
                spacing=6,
                controls=[
                    ft.Row(spacing=6, controls=[ft.Icon(icono, color=color, size=18), etiqueta(titulo)]),
                    self.valor,
                    self.barra,
                ],
            ),
        )

    def mostrar(self, valor, fraccion: float | None = None) -> None:
        self.valor.value = "—" if valor is None else f"{valor}{self.unidad}"
        if fraccion is not None:
            self.barra.value = max(0.0, min(1.0, fraccion))


class AnilloVirtual(ft.Stack):
    """Copia en pantalla del anillo NeoPixel del ESP32 (gemelo digital)."""

    def __init__(self, pixeles: int = 16, tam: int = 168, punto: int = 18):
        radio = (tam - punto) / 2 - 6
        centro = (tam - punto) / 2
        self.puntos = []
        for i in range(pixeles):
            angulo = -math.pi / 2 + i * 2 * math.pi / pixeles
            self.puntos.append(
                ft.Container(
                    width=punto,
                    height=punto,
                    border_radius=punto / 2,
                    bgcolor=APAGADO,
                    left=centro + radio * math.cos(angulo),
                    top=centro + radio * math.sin(angulo),
                )
            )
        self.texto = ft.Text("—", size=26, weight=ft.FontWeight.W_800, color=ft.Colors.WHITE)
        self.subtitulo = ft.Text("anillo", size=11, color="#9FB0D0")
        super().__init__(
            width=tam,
            height=tam,
            controls=[
                ft.Container(width=tam, height=tam, border_radius=tam / 2, bgcolor="#0E1A33"),
                *self.puntos,
                ft.Container(
                    width=tam,
                    height=tam,
                    left=0,
                    top=0,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Column(
                        tight=True,
                        spacing=0,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[self.texto, self.subtitulo],
                    ),
                ),
            ],
        )

    def mostrar(self, colores: list[str], texto: str, subtitulo: str) -> None:
        for punto, color in zip(self.puntos, colores):
            encendido = color.lower() != "#000000"
            punto.bgcolor = color if encendido else APAGADO
            # Flet usa #AARRGGBB: un halo semitransparente del mismo color.
            punto.shadow = ft.BoxShadow(blur_radius=10, color="#AA" + color[1:]) if encendido else None
        self.texto.value = texto
        self.subtitulo.value = subtitulo


def formatear_tiempo(segundos) -> str | None:
    if segundos is None:
        return None
    horas, resto = divmod(int(segundos), 3600)
    minutos, seg = divmod(resto, 60)
    return f"{horas}h {minutos:02d}m" if horas else f"{minutos}m {seg:02d}s"


def nombre_de_color(rgb: list[int]) -> str | None:
    for nombre, (_, valor) in COLORES.items():
        if list(valor) == list(rgb):
            return nombre
    return None


async def main(page: ft.Page):
    page.title = "Panel IoT UNAH · ESP32"
    page.bgcolor = FONDO
    page.padding = 0
    page.scroll = ft.ScrollMode.AUTO
    page.theme = ft.Theme(color_scheme_seed=AZUL_CLARO)
    page.theme_mode = ft.ThemeMode.LIGHT
    page.horizontal_alignment = ft.CrossAxisAlignment.STRETCH

    # Lo último que la interfaz sabe del LED, para decidir qué pide el botón.
    led_encendido = False

    # ---------- Encabezado ----------
    punto_estado = ft.Container(width=10, height=10, border_radius=5, bgcolor=ORO)
    texto_estado = ft.Text("Buscando al ESP32…", color=ft.Colors.WHITE, weight=ft.FontWeight.W_600)
    chip_estado = ft.Container(
        bgcolor="#26FFFFFF",
        border_radius=30,
        padding=ft.Padding.symmetric(horizontal=14, vertical=8),
        content=ft.Row([punto_estado, texto_estado], spacing=8, tight=True),
    )

    encabezado = ft.Container(
        gradient=ft.LinearGradient(colors=[AZUL, AZUL_CLARO]),
        padding=ft.Padding.symmetric(horizontal=28, vertical=20),
        content=ft.ResponsiveRow(
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Row(
                    col={"xs": 12, "md": 8},
                    spacing=20,
                    controls=[
                        ft.Container(
                            bgcolor=ft.Colors.WHITE,
                            border_radius=14,
                            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                            content=ft.Image(src="unah.png", height=56, fit=ft.BoxFit.CONTAIN),
                        ),
                        ft.Column(
                            spacing=2,
                            controls=[
                                ft.Text("Panel IoT · ESP32", size=26, weight=ft.FontWeight.W_800, color=ft.Colors.WHITE),
                                ft.Text(
                                    "Paradigmas de Programación · Flet + ESP32 en Wokwi por MQTT o HTTP",
                                    color="#C9D6F0",
                                ),
                            ],
                        ),
                    ],
                ),
                ft.Row(
                    col={"xs": 12, "md": 4},
                    alignment=ft.MainAxisAlignment.END,
                    controls=[chip_estado],
                ),
            ],
        ),
    )

    # ---------- Conexión ----------
    def crear_conexion() -> Conexion:
        if modo.selected == ["http"]:
            return ConexionHTTP(campo_url.value)
        return ConexionMQTT(campo_broker.value, campo_codigo.value)

    modo = ft.SegmentedButton(
        selected=[os.getenv("ESP32_MODO", "mqtt")],
        show_selected_icon=False,
        segments=[
            ft.Segment(value="mqtt", label="MQTT", icon=ft.Icons.HUB),
            ft.Segment(value="http", label="HTTP directo", icon=ft.Icons.ROUTER),
        ],
        on_change=lambda e: mostrar_campos_de_modo(),
    )
    campo_broker = ft.TextField(
        label="Broker MQTT (host:puerto)",
        value=os.getenv("MQTT_BROKER", MQTT_BROKER_POR_DEFECTO),
        prefix_icon=ft.Icons.HUB,
        border=ft.OutlineInputBorder(border_radius=12),
        dense=True,
        width=300,
        on_submit=lambda e: conectar(),
    )
    campo_codigo = ft.TextField(
        label="Código del ESP32",
        value=os.getenv("MQTT_CODIGO", CODIGO_POR_DEFECTO),
        prefix_icon=ft.Icons.KEY,
        border=ft.OutlineInputBorder(border_radius=12),
        dense=True,
        width=220,
        on_submit=lambda e: conectar(),
    )
    campo_url = ft.TextField(
        label="Dirección del servidor web del ESP32",
        value=os.getenv("ESP32_URL", URL_POR_DEFECTO),
        prefix_icon=ft.Icons.ROUTER,
        border=ft.OutlineInputBorder(border_radius=12),
        dense=True,
        width=360,
        on_submit=lambda e: conectar(),
    )
    detalle_conexion = ft.Text(size=12, color=TEXTO_SUAVE)

    async def abrir_panel_esp32(e):
        await ft.UrlLauncher().launch_url(campo_url.value.rstrip("/") + "/")

    boton_pagina = ft.OutlinedButton(
        "Abrir la página del ESP32",
        icon=ft.Icons.OPEN_IN_NEW,
        on_click=abrir_panel_esp32,
    )

    def ayuda_de_modo() -> str:
        if modo.selected == ["http"]:
            return ("HTTP directo: en Wokwi necesita F1 → «Enable Private Wokwi IoT Gateway» "
                    "con wokwigw abierto; localhost:9080 llega al puerto 80 del ESP32.")
        return ("MQTT: el ESP32 y esta app se conectan al mismo broker. El código debe ser "
                "igual al CODIGO de wokwi/main.py.")

    def mostrar_campos_de_modo():
        http = modo.selected == ["http"]
        campo_url.visible = boton_pagina.visible = http
        campo_broker.visible = campo_codigo.visible = not http
        detalle_conexion.value = ayuda_de_modo()
        detalle_conexion.color = TEXTO_SUAVE

    def conectar():
        nonlocal esp
        anterior, esp = esp, crear_conexion()

        async def cerrar_anterior():
            await asyncio.sleep(5)  # por si el ciclo todavía la está usando
            await anterior.cerrar()

        page.run_task(cerrar_anterior)
        bitacora.value = ""
        texto_estado.value = "Buscando al ESP32…"
        punto_estado.bgcolor = ORO
        detalle_conexion.value = ayuda_de_modo()
        detalle_conexion.color = TEXTO_SUAVE

    barra_conexion = ft.Container(
        bgcolor=ft.Colors.WHITE,
        border_radius=16,
        padding=ft.Padding.symmetric(horizontal=20, vertical=14),
        shadow=ft.BoxShadow(blur_radius=16, color="#120B2E6B", offset=ft.Offset(0, 4)),
        content=ft.Column(
            spacing=10,
            controls=[
                ft.Row(
                    spacing=12,
                    run_spacing=10,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        modo,
                        campo_broker,
                        campo_codigo,
                        campo_url,
                        ft.FilledButton("Conectar", icon=ft.Icons.LINK, on_click=lambda e: conectar()),
                        boton_pagina,
                    ],
                ),
                detalle_conexion,
            ],
        ),
    )
    mostrar_campos_de_modo()
    esp: Conexion = crear_conexion()

    # ---------- Controles ----------
    nombre = ft.TextField(
        label="Nombre",
        prefix_icon=ft.Icons.PERSON_OUTLINE,
        border=ft.OutlineInputBorder(border_radius=12),
        max_length=16,
        helper="Aparece en la pantalla OLED del ESP32",
        on_change=lambda e: esp.pedir(nombre=e.control.value.strip()),
    )

    aviso_terminos = ft.Text(
        "Acepta los términos para habilitar el control del ESP32.",
        size=12,
        color="#B26A00",
    )

    luz_led = ft.Container(width=46, height=46, border_radius=23, animate=300)
    estado_led = ft.Text("Sin confirmación del ESP32", size=12, color=TEXTO_SUAVE)
    boton_led = ft.FilledButton(icon=ft.Icons.POWER_SETTINGS_NEW, on_click=lambda e: cambiar_led())

    def pintar_led(encendido: bool):
        nonlocal led_encendido
        led_encendido = encendido
        luz_led.bgcolor = "#FF3B30" if encendido else "#D5DAE3"
        luz_led.shadow = (
            ft.BoxShadow(blur_radius=26, spread_radius=4, color="#99FF3B30") if encendido else None
        )
        boton_led.content = "Apagar LED" if encendido else "Encender LED"
        boton_led.bgcolor = "#C62828" if encendido else AZUL_CLARO

    def cambiar_led():
        esp.pedir(led=not led_encendido)
        pintar_led(not led_encendido)

    valor_slider = ft.Text("50%", color=ft.Colors.WHITE, weight=ft.FontWeight.W_700)
    angulo_slider = ft.Text("90°", size=12, color=TEXTO_SUAVE)

    def pintar_slider(valor: int):
        valor_slider.value = f"{valor}%"
        angulo_slider.value = f"servo {valor * 180 // 100}°"

    def mover_slider(e):
        valor = int(e.control.value)
        pintar_slider(valor)
        esp.pedir(nivel=valor)

    slider = ft.Slider(
        min=0,
        max=100,
        divisions=20,
        value=50,
        label="{value}%",
        on_change=mover_slider,
    )

    muestra_color = ft.Container(width=22, height=22, border_radius=11, bgcolor=COLORES["Azul UNAH"][0])

    def elegir_color(e):
        hex_color, rgb = COLORES[e.control.value]
        muestra_color.bgcolor = hex_color
        esp.pedir(color=e.control.value, rgb=list(rgb))

    color = ft.Dropdown(
        label="Color favorito (LED RGB y anillo)",
        value="Azul UNAH",
        expand=True,
        border=ft.OutlineInputBorder(border_radius=12),
        options=[
            ft.DropdownOption(
                key=nombre_color,
                text=nombre_color,
                leading_icon=ft.Container(width=14, height=14, border_radius=7, bgcolor=hex_color),
            )
            for nombre_color, (hex_color, _) in COLORES.items()
        ],
        on_select=elegir_color,
    )

    efecto = ft.SegmentedButton(
        selected=["fijo"],
        show_selected_icon=False,
        segments=[
            ft.Segment(value="fijo", label="Nivel", icon=ft.Icons.LINEAR_SCALE),
            ft.Segment(value="respirar", label="Respirar", icon=ft.Icons.AIR),
            ft.Segment(value="arcoiris", label="Arcoíris", icon=ft.Icons.GRADIENT),
            ft.Segment(value="termometro", label="Termómetro", icon=ft.Icons.THERMOSTAT),
        ],
        on_change=lambda e: esp.pedir(efecto=e.control.selected[0]),
    )

    framework = ft.RadioGroup(
        value="Flet",
        on_change=lambda e: esp.pedir(framework=e.control.value),
        content=ft.Row(spacing=4, wrap=True, controls=[ft.Radio(value=f, label=f) for f in FRAMEWORKS]),
    )

    notificaciones = ft.Switch(
        label="Habilitar notificaciones (zumbador y avisos en pantalla)",
        value=True,
        on_change=lambda e: esp.pedir(notificaciones=e.control.value),
    )

    mensaje = ft.TextField(
        label="Mensaje para la OLED",
        hint_text="Vacío = mostrar la IP",
        prefix_icon=ft.Icons.TEXT_FIELDS,
        border=ft.OutlineInputBorder(border_radius=12),
        max_length=32,
        expand=True,
        on_submit=lambda e: enviar_mensaje(),
    )

    def enviar_mensaje():
        esp.pedir(mensaje=mensaje.value.strip())

    boton_mensaje = ft.IconButton(
        icon=ft.Icons.SEND,
        icon_color=AZUL_CLARO,
        tooltip="Enviar a la OLED",
        on_click=lambda e: enviar_mensaje(),
    )
    boton_melodia = ft.OutlinedButton(
        "Tocar melodía",
        icon=ft.Icons.MUSIC_NOTE,
        on_click=lambda e: esp.pedir(accion="melodia"),
    )
    boton_contador = ft.TextButton(
        "Reiniciar contador del botón",
        icon=ft.Icons.RESTART_ALT,
        on_click=lambda e: esp.pedir(accion="reiniciar_contador"),
    )

    controles_esp32 = [
        nombre, boton_led, slider, color, efecto, framework, notificaciones,
        mensaje, boton_mensaje, boton_melodia, boton_contador,
    ]

    def aplicar_terminos(aceptados: bool):
        for control in controles_esp32:
            control.disabled = not aceptados
        aviso_terminos.visible = not aceptados

    terminos = ft.Checkbox(
        label="Acepto los términos y condiciones",
        on_change=lambda e: aplicar_terminos(e.control.value),
    )

    seccion_led = ft.Container(
        bgcolor="#F6F8FC",
        border_radius=16,
        padding=16,
        content=ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[
                ft.Row(
                    spacing=14,
                    controls=[
                        luz_led,
                        ft.Column(
                            spacing=2,
                            controls=[
                                ft.Text("LED rojo · GPIO 26", weight=ft.FontWeight.W_600, color=AZUL),
                                estado_led,
                            ],
                        ),
                    ],
                ),
                boton_led,
            ],
        ),
    )

    panel_controles = tarjeta(
        "Controles",
        ft.Icons.TUNE,
        [
            ft.Text("Completa el formulario y ajusta las opciones:", color=TEXTO_SUAVE),
            nombre,
            terminos,
            aviso_terminos,
            seccion_led,
            ft.Column(
                spacing=0,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            etiqueta("NIVEL · SERVO GPIO 18 Y ANILLO GPIO 13"),
                            ft.Row(
                                spacing=8,
                                controls=[
                                    angulo_slider,
                                    ft.Container(
                                        bgcolor=AZUL_CLARO,
                                        border_radius=8,
                                        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                                        content=valor_slider,
                                    ),
                                ],
                            ),
                        ],
                    ),
                    slider,
                ],
            ),
            ft.Row([muestra_color, color], spacing=12),
            ft.Column([etiqueta("EFECTO DEL ANILLO NEOPIXEL"), efecto], spacing=8),
            ft.Column([etiqueta("FRAMEWORK FAVORITO"), framework], spacing=4),
            notificaciones,
            ft.Row([mensaje, boton_mensaje], vertical_alignment=ft.CrossAxisAlignment.START),
            ft.Row([boton_melodia, boton_contador], wrap=True),
        ],
        col={"xs": 12, "lg": 5},
    )

    # ---------- Datos que llegan del ESP32 ----------
    temperatura = Indicador("Temperatura", ft.Icons.THERMOSTAT, "#E53935", " °C")
    humedad = Indicador("Humedad", ft.Icons.WATER_DROP_OUTLINED, "#1E88E5", " %", barra=True)
    potenciometro = Indicador("Potenciómetro", ft.Icons.TONALITY, "#8E24AA", " %", barra=True)
    pulsaciones = Indicador("Botón «Avisar»", ft.Icons.TOUCH_APP_OUTLINED, ORO, " veces")
    senal = Indicador("Señal WiFi", ft.Icons.WIFI, "#43A047", " dBm")
    activo = Indicador("Tiempo activo", ft.Icons.TIMER_OUTLINED, AZUL_CLARO)

    anillo = AnilloVirtual()

    def dato(titulo: str) -> tuple[ft.Text, ft.Row]:
        valor = ft.Text("—", weight=ft.FontWeight.W_600, color=AZUL, font_family="monospace")
        return valor, ft.Row(
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            controls=[ft.Text(titulo, size=13, color=TEXTO_SUAVE), valor],
        )

    dato_ip, fila_ip = dato("IP del ESP32")
    dato_servo, fila_servo = dato("Ángulo del servo")
    dato_oled, fila_oled = dato("Texto en la OLED")
    dato_memoria, fila_memoria = dato("Memoria libre")
    dato_peticiones, fila_peticiones = dato("Peticiones atendidas")
    dato_latencia, fila_latencia = dato("Tiempo de respuesta")

    gemelo = ft.Container(
        bgcolor="#F6F8FC",
        border_radius=16,
        padding=16,
        border=ft.Border.all(1, "#E3E8F2"),
        content=ft.ResponsiveRow(
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Container(col={"xs": 12, "sm": 5}, alignment=ft.Alignment.CENTER, content=anillo),
                ft.Column(
                    col={"xs": 12, "sm": 7},
                    spacing=8,
                    controls=[
                        etiqueta("ASÍ SE VE EL ESP32 AHORA"),
                        fila_ip, fila_servo, fila_oled, fila_memoria, fila_peticiones, fila_latencia,
                    ],
                ),
            ],
        ),
    )

    bitacora = ft.TextField(
        label="Mensajes recibidos del ESP32",
        read_only=True,
        multiline=True,
        min_lines=8,
        max_lines=8,
        border=ft.OutlineInputBorder(border_radius=12),
        text_style=ft.TextStyle(font_family="monospace", size=13),
        value="",
        hint_text="Todavía no llega nada. Inicia la simulación en Wokwi.",
    )
    ultima_sync = ft.Text(size=12, color=TEXTO_SUAVE)

    panel_datos = tarjeta(
        "Datos del ESP32",
        ft.Icons.SENSORS,
        [
            ft.ResponsiveRow(
                spacing=12,
                run_spacing=12,
                controls=[temperatura, humedad, potenciometro, pulsaciones, senal, activo],
            ),
            gemelo,
            bitacora,
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                wrap=True,
                controls=[
                    ultima_sync,
                    ft.Text(
                        "GET /api/estado · POST /api/control",
                        size=12,
                        color=TEXTO_SUAVE,
                        font_family="monospace",
                    ),
                ],
            ),
        ],
        col={"xs": 12, "lg": 7},
    )

    pintar_led(False)
    pintar_slider(50)
    aplicar_terminos(False)

    page.add(
        encabezado,
        ft.Container(
            padding=ft.Padding.only(left=24, right=24, top=20),
            content=barra_conexion,
        ),
        ft.Container(
            padding=ft.Padding.symmetric(horizontal=24, vertical=20),
            content=ft.ResponsiveRow(
                spacing=24,
                run_spacing=24,
                controls=[panel_controles, panel_datos],
            ),
        ),
    )

    # ---------- Mostrar lo que responde el ESP32 ----------
    def adoptar_salidas(s: dict):
        """Refleja en los controles el estado real del ESP32.

        Así, si el LED se cambia desde la página del propio ESP32, la app
        también se entera. Se respetan los controles que el usuario acaba
        de tocar, para no regresarlos a un valor viejo.
        """
        if not esp.recien_editado("led"):
            pintar_led(bool(s["led"]))
        if not esp.recien_editado("nivel") and slider.value != s["nivel"]:
            slider.value = s["nivel"]
            pintar_slider(s["nivel"])
        if not esp.recien_editado("rgb"):
            nombre_color = nombre_de_color(s["rgb"])
            if nombre_color and color.value != nombre_color:
                color.value = nombre_color
                muestra_color.bgcolor = COLORES[nombre_color][0]
        if not esp.recien_editado("efecto") and efecto.selected != [s["efecto"]]:
            efecto.selected = [s["efecto"]]
        if not esp.recien_editado("framework") and framework.value != s["framework"]:
            framework.value = s["framework"]
        if not esp.recien_editado("notificaciones") and notificaciones.value != s["notificaciones"]:
            notificaciones.value = s["notificaciones"]
        if not esp.recien_editado("nombre") and nombre.value.strip() != s["nombre"]:
            nombre.value = s["nombre"]

    def agregar_eventos(datos: dict):
        lineas = []
        avisos = []
        ahora = datetime.now()
        for evento in datos.get("eventos", []):
            # `t` son segundos desde que arrancó el ESP32; se pasa a hora local.
            hora = ahora - timedelta(seconds=max(0, datos["tiempo_activo"] - evento["t"]))
            marca = {"aviso": "▲", "sistema": "◆"}.get(evento.get("tipo"), "•")
            lineas.append(f"[{hora:%H:%M:%S}] {marca} {evento['texto']}")
            # Sólo se notifican avisos recientes, no el historial al conectar.
            if evento.get("tipo") == "aviso" and datos["tiempo_activo"] - evento["t"] <= 5:
                avisos.append(evento["texto"])
        if lineas:
            todas = list(reversed(lineas)) + (bitacora.value.splitlines() if bitacora.value else [])
            bitacora.value = "\n".join(todas[:MAX_LINEAS_BITACORA])
        if avisos and notificaciones.value:
            page.show_dialog(
                ft.SnackBar(
                    ft.Row(
                        [ft.Icon(ft.Icons.CAMPAIGN, color=ORO), ft.Text(f"ESP32: {avisos[-1]}", color=ft.Colors.WHITE)],
                        spacing=10,
                    ),
                    bgcolor=AZUL,
                    duration=2500,
                )
            )

    def mostrar_datos(datos: dict):
        latencia = "—" if esp.latencia_ms is None else f"{esp.latencia_ms:.0f} ms"
        punto_estado.bgcolor = "#4CD964"
        texto_estado.value = f"ESP32 en línea · {esp.nombre} · {latencia}"
        detalle_conexion.value = f"Conectado por {esp.nombre}: {esp.descripcion}"
        detalle_conexion.color = VERDE

        sensores = datos["sensores"]
        t = sensores.get("temperatura")
        temperatura.mostrar(None if t is None else f"{t:.1f}")
        humedad.mostrar(sensores.get("humedad"), (sensores.get("humedad") or 0) / 100)
        potenciometro.mostrar(sensores.get("potenciometro"), (sensores.get("potenciometro") or 0) / 100)
        pulsaciones.mostrar(sensores.get("pulsaciones"))
        senal.mostrar(datos.get("rssi"))
        activo.mostrar(formatear_tiempo(datos.get("tiempo_activo")))

        s = datos["salidas"]
        adoptar_salidas(s)
        estado_led.value = "El ESP32 confirma: " + ("encendido" if s["led"] else "apagado")

        anillo.mostrar(datos.get("anillo", []), f"{s['nivel']}%", EFECTOS.get(s["efecto"], s["efecto"]))
        dato_ip.value = datos.get("ip", "—")
        dato_servo.value = f"{datos.get('servo_grados', '—')}°"
        dato_oled.value = s.get("mensaje") or f"Hola {s.get('nombre') or 'invitado'}"
        dato_memoria.value = f"{datos.get('memoria_libre', 0) / 1024:.1f} KB"
        dato_peticiones.value = str(datos.get("peticiones", "—"))
        dato_latencia.value = latencia

        if datos.get("reiniciado"):
            bitacora.value = ""
        agregar_eventos(datos)
        ultima_sync.value = f"Última respuesta: {datetime.now():%H:%M:%S}"

    def mostrar_error(error: Exception):
        punto_estado.bgcolor = ROJO
        texto_estado.value = "ESP32 sin conexión"
        if isinstance(error, SinConexion):
            motivo = str(error)
        else:
            motivo = f"Respuesta inesperada del ESP32 ({error.__class__.__name__}: {error})"
        if esp.nombre == "HTTP":
            pista = "¿Está corriendo la simulación con el Private IoT Gateway (wokwigw) activo?"
        else:
            pista = "¿Está corriendo la simulación en Wokwi con el mismo broker y código?"
        detalle_conexion.value = f"{motivo}. {pista}"
        detalle_conexion.color = ROJO

    # ---------- Ciclo de sincronización ----------
    sesion_abierta = True

    def al_cerrar(e):
        nonlocal sesion_abierta
        sesion_abierta = False

    page.on_close = al_cerrar

    async def sincronizar():
        while sesion_abierta:
            try:
                mostrar_datos(await esp.sincronizar())
                pausa = INTERVALO
            except Exception as error:  # SinConexion o un JSON con otra forma
                mostrar_error(error)
                pausa = 1.0
            try:
                page.update()
            except Exception:
                break  # la sesión ya se cerró
            # Esperar, pero salir antes si el usuario cambió algo.
            fin = time.monotonic() + pausa
            while sesion_abierta and time.monotonic() < fin:
                if esp.hay_pendientes() and pausa == INTERVALO:
                    await asyncio.sleep(0.1)  # agrupa cambios seguidos (slider)
                    break
                await asyncio.sleep(0.05)
        await esp.cerrar()

    page.run_task(sincronizar)


if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
