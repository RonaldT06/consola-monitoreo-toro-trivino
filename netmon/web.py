"""API REST + WebSocket + archivos del dashboard.

Rol de este módulo en el sistema:
  Es la INTERFAZ WEB de NetMon, construida con FastAPI. Ofrece tres cosas:

  1. Archivos estáticos: la página del dashboard (static/index.html, JavaScript y CSS).
  2. API REST (HTTP + JSON), por ejemplo:
       GET  /api/state          -> estado completo de todos los equipos
       GET  /api/events         -> historial de eventos (desde SQLite)
       POST /api/poll/{equipo}  -> forzar un sondeo inmediato
       POST /api/telegram/test  -> enviar un mensaje de prueba a Telegram
       POST /api/sim/...        -> controles del simulador (apagar equipo, tumbar interfaz, pico de CPU)
     La documentación interactiva de la API queda en /api/docs.
  3. WebSocket (/ws): conexión permanente con cada navegador abierto. En vez de que la página
     pregunte cada pocos segundos ("polling"), el servidor EMPUJA los cambios apenas ocurren
     (eventos y estado), así el dashboard se actualiza en tiempo real.

  - Recibe: el Monitor (para leer el estado y suscribirse a sus cambios) y, opcionalmente, el
    bot de Telegram.
  - Produce: la aplicación FastAPI que run.py sirve con uvicorn.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .monitor import Monitor
from .sim import SimDriver

# Carpeta "static" que está junto a este archivo (contiene index.html, JS y CSS del dashboard).
STATIC = Path(__file__).parent / "static"


def create_app(mon: Monitor, telegram=None) -> FastAPI:
    """Crea y configura la aplicación web.

    Las rutas se definen como funciones internas (closures) para que puedan usar directamente
    las variables "mon", "telegram" y "clients" sin variables globales.

    Parámetros:
        mon:      el Monitor del que se obtienen estados, eventos y acciones.
        telegram: el bot de Telegram, o None si está deshabilitado.

    Retorna:
        La aplicación FastAPI lista para servir.
    """
    app = FastAPI(title="NetMon", docs_url="/api/docs")
    # Conjunto de navegadores conectados por WebSocket en este momento.
    clients: set[WebSocket] = set()
    # Bandera de "ya hay un envío de estado programado". Se usa un diccionario (y no una
    # variable booleana suelta) para poder modificarla desde las funciones internas sin
    # declarar "nonlocal".
    pending = {"state": False}

    async def broadcast(msg: dict):
        """Envía un mensaje JSON a TODOS los navegadores conectados por WebSocket.

        Parámetros:
            msg: diccionario a enviar; se convierte a texto JSON una sola vez para todos.
        """
        # default=str convierte a texto cualquier valor que JSON no sepa serializar.
        data = json.dumps(msg, default=str)
        # Se recorre una COPIA (list) porque el conjunto puede modificarse durante el envío.
        for ws in list(clients):
            try:
                await ws.send_text(data)
            except Exception:
                # El navegador se cerró o perdió conexión: se saca de la lista.
                clients.discard(ws)

    async def on_event(ev: dict):
        """Listener de eventos del Monitor: reenvía cada evento nuevo a los navegadores.

        Parámetros:
            ev: evento creado por Monitor.emit.
        """
        await broadcast({"type": "event", "event": ev})

    async def on_state():
        """Envía el estado completo a los navegadores, agrupando actualizaciones seguidas.

        DEBOUNCE (anti-rebote) de 0,3 s: el monitor avisa un cambio de estado cada vez que termina
        el sondeo de CADA equipo. Con varios equipos, esos avisos llegan casi juntos. En lugar de
        enviar el estado completo (que puede ser grande) varias veces, se espera 0,3 s y se envía
        una sola vez con todos los cambios acumulados. Los avisos que llegan durante la espera se
        ignoran porque ya quedarán incluidos en ese envío.
        """
        # agrupa varias actualizaciones seguidas en un solo envío
        if pending["state"]:
            return
        pending["state"] = True
        await asyncio.sleep(0.3)
        pending["state"] = False
        await broadcast({"type": "state", "state": mon.state()})

    # Suscribe la web a los eventos del Monitor (igual que lo hace Telegram).
    mon.event_listeners.append(on_event)
    async def schedule_state():
        """Listener de estado del Monitor: programa on_state() como tarea independiente.

        Se usa create_task para NO hacer esperar al bucle de sondeo del monitor durante los
        0,3 s del debounce: el monitor sigue con su trabajo y el envío ocurre en paralelo.
        """
        asyncio.create_task(on_state())

    mon.state_listeners.append(schedule_state)

    # El decorador @app.get / @app.post asocia cada función a una URL y un método HTTP.
    @app.get("/")
    async def index():
        """Sirve la página principal del dashboard (static/index.html)."""
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        """GET /api/state: estado completo de todos los equipos y alertas activas (JSON).

        Retorna:
            El diccionario de Monitor.state(); FastAPI lo convierte a JSON automáticamente.
        """
        return mon.state()

    @app.get("/api/events")
    async def events(limit: int = 200, device: str | None = None):
        """GET /api/events?limit=N&device=X: historial de eventos guardados en SQLite.

        Parámetros (de la URL):
            limit:  cantidad máxima de eventos (se limita a 2000 para no sobrecargar).
            device: filtrar por nombre de equipo (opcional).

        Retorna:
            Lista de eventos, del más reciente al más antiguo.
        """
        return mon.store.recent(min(limit, 2000), device)

    @app.post("/api/poll/{name}")
    async def poll(name: str):
        """POST /api/poll/{name}: fuerza un sondeo inmediato del equipo indicado.

        Parámetros:
            name: nombre del equipo (parte de la URL).

        Retorna:
            {"ok": True}, o error HTTP 404 si el equipo no existe.
        """
        if name not in mon.devices:
            raise HTTPException(404, "equipo no existe")
        mon.poll_now(name)
        return {"ok": True}

    @app.post("/api/telegram/test")
    async def tg_test():
        """POST /api/telegram/test: envía un mensaje de prueba a los chats de Telegram.

        Retorna:
            {"ok": True}, o error HTTP 400 si Telegram no está habilitado.
        """
        if not telegram:
            raise HTTPException(400, "Telegram no está habilitado en config.yaml")
        await telegram.send("✅ Mensaje de prueba desde el dashboard NetMon")
        return {"ok": True}

    # ---------------- controles del simulador ----------------
    def sim(name: str) -> SimDriver:
        """Obtiene el equipo simulado con ese nombre (función auxiliar de las rutas /api/sim).

        Parámetros:
            name: nombre del equipo.

        Retorna:
            El SimDriver del equipo. Lanza HTTP 400 si el equipo no existe o si NetMon no está
            en modo simulador (con equipos reales estas acciones no tienen sentido).
        """
        ds = mon.devices.get(name)
        if not ds or not isinstance(ds.driver, SimDriver):
            raise HTTPException(400, "Solo disponible en modo simulador")
        return ds.driver

    @app.post("/api/sim/{name}/power")
    async def sim_power(name: str):
        """POST /api/sim/{name}/power: apaga o enciende el equipo simulado.

        Retorna:
            {"powered": True/False} con el nuevo estado de energía.
        """
        on = sim(name).toggle_power()
        # Sondeo inmediato para que el cambio se vea sin esperar el intervalo. Ojo: para
        # declarar el equipo caído siguen haciendo falta "down_after" fallos seguidos.
        mon.poll_now(name)
        return {"powered": on}

    @app.post("/api/sim/{name}/iface/{idx}/{action}")
    async def sim_iface(name: str, idx: str, action: str):
        """POST /api/sim/{name}/iface/{idx}/{action}: cambia el estado de una interfaz simulada.

        Parámetros:
            name:   nombre del equipo.
            idx:    ifIndex de la interfaz.
            action: "admin" = shutdown / no shutdown; "link" = desconectar / conectar el cable.

        Retorna:
            {"ok": True}, o error HTTP 404/400 si la interfaz o la acción no son válidas.
        """
        d = sim(name)
        if idx not in d.ifaces:
            raise HTTPException(404, "interfaz no existe")
        if action == "admin":
            d.toggle_admin(idx)
        elif action == "link":
            d.toggle_link(idx)
        else:
            raise HTTPException(400, "acción: admin | link")
        mon.poll_now(name)
        return {"ok": True}

    @app.post("/api/sim/{name}/spike")
    async def sim_spike(name: str):
        """POST /api/sim/{name}/spike: provoca un pico de CPU y memoria de 60 segundos.

        Sirve para probar las alertas de umbral (y su histéresis) sin equipos reales.

        Retorna:
            {"ok": True}
        """
        sim(name).spike(60)
        mon.poll_now(name)
        return {"ok": True}

    # ---------------- WebSocket ----------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        """Atiende la conexión WebSocket de un navegador durante toda su vida.

        Al conectarse, el navegador recibe de inmediato el estado actual y los últimos 100
        eventos (para no mostrar una página vacía). Después queda registrado en "clients" y
        recibe las actualizaciones que envía broadcast().

        Parámetros:
            ws: la conexión WebSocket entregada por FastAPI.
        """
        # Completa el "handshake": la conexión HTTP se convierte en WebSocket.
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_text(json.dumps({"type": "state", "state": mon.state()}, default=str))
            await ws.send_text(json.dumps({"type": "events", "events": mon.store.recent(100)}, default=str))
            # Bucle de lectura: el contenido recibido no se usa, pero leer es necesario para
            # detectar cuándo el navegador cierra la conexión (se lanza WebSocketDisconnect).
            while True:
                await ws.receive_text()  # mantiene la conexión; el cliente envía pings
        except WebSocketDisconnect:
            pass
        finally:
            # Pase lo que pase, el cliente se saca de la lista al terminar.
            clients.discard(ws)

    # Sirve los archivos de la carpeta static bajo la URL /static (JS, CSS, imágenes).
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
