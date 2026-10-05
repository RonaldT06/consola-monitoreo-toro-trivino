"""Notificaciones y comandos por Telegram (API HTTP de bots, sin librerías extra).

Comandos del bot:
  /estado            resumen de todos los equipos
  /interfaces <eq>   estado de interfaces de un equipo
  /alertas           alertas activas
  /ayuda

Rol de este módulo en el sistema:
  Es el canal de NOTIFICACIÓN hacia el celular del administrador. Hace dos trabajos en paralelo:

  1. ENVÍO (sender): el monitor le entrega cada evento (on_event); si su severidad alcanza el
     mínimo configurado, el mensaje se pone en una COLA y una tarea aparte lo envía a todos los
     chats autorizados. La cola desacopla al monitor de Telegram: el monitor no se queda
     esperando la red, y si Telegram está lento o limita el envío, los mensajes simplemente
     esperan su turno sin perderse.
  2. COMANDOS (poller): consulta a Telegram si hay mensajes nuevos (/estado, /alertas...) y
     responde con información tomada directamente del Monitor.

Se habla con Telegram mediante su API HTTP (https://api.telegram.org/bot<token>/<método>),
enviando JSON con la librería httpx en modo asíncrono.

Qué recibe: la sección "telegram" de la configuración y el objeto Monitor.
Qué produce: mensajes en los chats de Telegram configurados.
"""
from __future__ import annotations

import asyncio
import html
import time

import httpx

from .monitor import SEV_ORDER, Monitor, fmt_duration

# Plantilla de URL de la API de bots de Telegram: cada "método" es una acción
# (sendMessage, getUpdates, getMe, setMyCommands...).
API = "https://api.telegram.org/bot{token}/{method}"
# Icono que se muestra según el estado del equipo.
ICON = {"up": "🟢", "down": "🔴", "snmp_fail": "🟠", "unknown": "⚪"}


class Telegram:
    """Bot de Telegram: envía alertas del Monitor y responde comandos de consulta."""

    def __init__(self, cfg: dict, monitor: Monitor):
        """Prepara el bot (todavía no se conecta a Telegram).

        Parámetros:
            cfg:     sección "telegram" de la configuración (bot_token, chat_ids,
                     min_severity, commands).
            monitor: el Monitor, de donde se leen los estados para responder comandos.
        """
        self.cfg = cfg
        self.mon = monitor
        self.token = cfg["bot_token"]
        self.chat_ids = cfg["chat_ids"]   # chats autorizados (reciben alertas y pueden dar comandos)
        # Severidad mínima a enviar, convertida a número (info=0, warning=1, critical=2).
        self.min_sev = SEV_ORDER.get(cfg.get("min_severity", "info"), 0)
        # Cola asíncrona de mensajes pendientes por enviar (productor: on_event/send;
        # consumidor: sender).
        self.queue: asyncio.Queue = asyncio.Queue()
        # Cliente HTTP reutilizable. El timeout (40 s) es mayor que los 25 s del long polling
        # de getUpdates, para que la petición no se corte antes de que Telegram responda.
        self.http = httpx.AsyncClient(timeout=40)
        # "offset" de getUpdates: identificador del próximo mensaje que se quiere recibir.
        self.offset = 0

    async def _call(self, method: str, **data):
        """Llama a un método de la API de Telegram y devuelve su resultado.

        Parámetros:
            method: nombre del método (ej. "sendMessage").
            **data: parámetros del método; se envían como JSON en el cuerpo del POST.

        Retorna:
            El campo "result" de la respuesta.

        Lanza RuntimeError con el cuerpo de la respuesta si Telegram indica "ok": false
        (así quien llama puede revisar, por ejemplo, "retry_after").
        """
        r = await self.http.post(API.format(token=self.token, method=method), json=data)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body)
        return body["result"]

    # ------------------------------------------------------------ envío
    async def on_event(self, ev: dict):
        """Listener que el Monitor llama con cada evento nuevo.

        Si la severidad del evento es igual o mayor que la mínima configurada, se encola el
        mensaje con la fecha y hora en cursiva. No envía directamente: eso lo hace sender().

        Parámetros:
            ev: evento creado por Monitor.emit (ts, device, severity, kind, message, id).
        """
        if SEV_ORDER.get(ev["severity"], 0) >= self.min_sev:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev["ts"]))
            await self.queue.put(f"{ev['message']}\n<i>{ts}</i>")

    async def send(self, text: str, chat_id: int | None = None):
        """Encola un mensaje para enviarlo.

        Parámetros:
            text:    texto en HTML simple.
            chat_id: si se indica, el mensaje va solo a ese chat (ej. la respuesta a un comando);
                     si no, va a todos los chats configurados.

        En la cola se guarda una tupla (chat_id, texto) para un chat concreto, o solo el texto
        para "todos los chats".
        """
        await self.queue.put((chat_id, text) if chat_id else text)

    async def sender(self):
        """Tarea infinita que saca mensajes de la cola y los envía, con reintentos.

        Maneja dos tipos de problema:
          - Límite de velocidad (rate limiting): si se envían demasiados mensajes, Telegram
            responde error 429 con "retry_after" = segundos que hay que esperar. Se espera ese
            tiempo y se reintenta el MISMO mensaje.
          - Problemas de red (sin Internet, DNS...): se reintenta hasta 5 veces esperando cada
            vez más (3 s, 6 s, 9 s...).
        """
        while True:
            # get() se queda esperando (sin consumir CPU) hasta que haya un mensaje en la cola.
            item = await self.queue.get()
            # Tupla -> un chat específico; texto solo -> todos los chats autorizados.
            chats, text = ([item[0]], item[1]) if isinstance(item, tuple) else (self.chat_ids, item)
            for chat in chats:
                for attempt in range(5):
                    try:
                        # Telegram admite máx. 4096 caracteres por mensaje; se recorta a 4000.
                        # parse_mode="HTML" permite usar <b>, <i>, <code> en el texto.
                        await self._call("sendMessage", chat_id=chat, text=text[:4000],
                                         parse_mode="HTML", disable_web_page_preview=True)
                        break
                    except RuntimeError as e:
                        # Telegram respondió con error: se busca "parameters.retry_after".
                        info = e.args[0] if e.args else {}
                        wait = (info.get("parameters") or {}).get("retry_after") if isinstance(info, dict) else None
                        if wait:
                            await asyncio.sleep(wait)
                            continue
                        # Otro error (chat inexistente, HTML mal formado...): reintentar no sirve.
                        print(f"[telegram] error enviando a {chat}: {info}")
                        break
                    except Exception as e:  # red caída: reintenta
                        print(f"[telegram] sin conexión ({e.__class__.__name__}), reintento {attempt + 1}/5")
                        # Espera creciente entre reintentos (backoff lineal).
                        await asyncio.sleep(3 * (attempt + 1))
            # Pequeña pausa entre mensajes para no saturar la API.
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------ comandos
    def _estado(self) -> str:
        """Construye la respuesta del comando /estado.

        Retorna:
            Texto HTML con una línea por equipo (icono, nombre, IP) y, si está arriba, sus
            interfaces activas, CPU, memoria y uptime; si está caído, hace cuánto. Al final, el
            número de alertas activas.
        """
        lines = ["<b>📊 Estado de la red</b>"]
        for ds in self.mon.devices.values():
            s = ds.snap
            line = f"{ICON.get(ds.status, '⚪')} <b>{ds.name}</b> ({ds.cfg['host']})"
            if ds.status == "up":
                # Cuántas interfaces tienen enlace (oper=up) del total monitoreado.
                ups = sum(1 for i in ds.ifaces.values() if i["oper"] == "up")
                parts = [f"if {ups}/{len(ds.ifaces)} up"]
                if s.get("cpu") is not None:
                    parts.append(f"CPU {s['cpu']:.0f}%")
                if s.get("mem") is not None:
                    parts.append(f"Mem {s['mem']:.0f}%")
                if s.get("uptime") is not None:
                    parts.append(f"uptime {fmt_duration(s['uptime'])}")
                line += "\n    " + " · ".join(parts)
            elif ds.status == "down" and ds.down_since:
                line += f"\n    caído hace {fmt_duration(time.time() - ds.down_since)}"
            lines.append(line)
        lines.append(f"\nAlertas activas: {len(self.mon.active)}")
        return "\n".join(lines)

    def _interfaces(self, name: str) -> str:
        """Construye la respuesta del comando /interfaces <equipo>.

        Parámetros:
            name: nombre del equipo escrito por el usuario. Primero se busca coincidencia exacta
                  (sin distinguir mayúsculas) y, si no hay, coincidencia parcial.

        Retorna:
            Texto HTML con una línea por interfaz, o un mensaje de uso si no se encontró el equipo.
        """
        match = [d for d in self.mon.devices.values() if d.name.lower() == name.lower()] or \
                [d for d in self.mon.devices.values() if name.lower() in d.name.lower()]
        if not name or not match:
            # &lt; y &gt; son "<" y ">" escapados, porque el mensaje se interpreta como HTML.
            return "Uso: /interfaces &lt;equipo&gt;\nEquipos: " + ", ".join(self.mon.devices)
        ds = match[0]
        lines = [f"<b>🔌 Interfaces de {ds.name}</b>"]
        for i in ds.ifaces.values():
            # Verde = con enlace; negro = deshabilitada (shutdown); rojo = habilitada pero sin enlace.
            ic = "🟢" if i["oper"] == "up" else ("⚫" if i["admin"] == "down" else "🔴")
            lines.append(f"{ic} {html.escape(i['name'])} — {i['oper']}" + (" (shutdown)" if i["admin"] == "down" else ""))
        return "\n".join(lines)

    def _alertas(self) -> str:
        """Construye la respuesta del comando /alertas.

        Retorna:
            Texto HTML con cada alerta activa del Monitor y hace cuánto se generó, o un mensaje
            indicando que no hay alertas.
        """
        if not self.mon.active:
            return "✅ Sin alertas activas"
        return "<b>🚨 Alertas activas</b>\n" + "\n".join(
            f"• {e['message']} <i>(hace {fmt_duration(time.time() - e['ts'])})</i>" for e in self.mon.active.values()
        )

    async def poller(self):
        """Tarea infinita que recibe los mensajes enviados al bot y responde los comandos.

        Usa LONG POLLING con el método getUpdates: se hace una petición HTTP que Telegram deja
        "abierta" hasta 25 segundos; si llega un mensaje en ese tiempo, responde de inmediato,
        y si no, responde con una lista vacía. Así se reciben los comandos casi en tiempo real
        sin necesidad de abrir un puerto público (webhook) ni de preguntar a cada rato.
        """
        try:
            # Registra la lista de comandos que Telegram muestra en el menú "/" del chat.
            await self._call("setMyCommands", commands=[
                {"command": "estado", "description": "Resumen de equipos"},
                {"command": "interfaces", "description": "Interfaces de un equipo"},
                {"command": "alertas", "description": "Alertas activas"},
                {"command": "ayuda", "description": "Ayuda"},
            ])
        except Exception as e:
            print(f"[telegram] no se pudieron registrar comandos: {e}")
        while True:
            try:
                # offset = "dame solo los mensajes con update_id >= offset". Al avanzarlo,
                # Telegram da por leídos los anteriores y no los vuelve a enviar.
                updates = await self._call("getUpdates", offset=self.offset, timeout=25,
                                           allowed_updates=["message"])
            except Exception as e:
                print(f"[telegram] getUpdates: {e}")
                await asyncio.sleep(5)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat = (msg.get("chat") or {}).get("id")
                text = (msg.get("text") or "").strip()
                # Solo interesan los comandos (empiezan con "/"); el resto se ignora.
                if not text.startswith("/"):
                    continue
                # Seguridad: solo los chats de la lista pueden consultar el estado de la red.
                # A los demás se les dice su chat_id para que el administrador pueda agregarlos.
                if chat not in self.chat_ids:
                    await self.send(f"⛔ Chat no autorizado. Tu chat_id es <code>{chat}</code> "
                                    f"(agrégalo en config.yaml si corresponde)", chat)
                    continue
                # Separa el comando del argumento: "/interfaces SW1" -> "/interfaces", "SW1".
                cmd, _, arg = text.partition(" ")
                # En grupos el comando llega como "/estado@NombreDelBot"; se quita "@...".
                cmd = cmd.split("@")[0].lower()
                if cmd in ("/estado", "/status", "/start"):
                    reply = self._estado()
                elif cmd == "/interfaces":
                    reply = self._interfaces(arg.strip())
                elif cmd == "/alertas":
                    reply = self._alertas()
                else:
                    # Cualquier otro comando (incluido /ayuda) muestra la ayuda.
                    reply = ("<b>NetMon bot</b>\n/estado — resumen\n/interfaces &lt;equipo&gt;\n"
                             "/alertas — alertas activas")
                await self.send(reply, chat)

    async def run(self):
        """Punto de arranque del bot: verifica el token, avisa el inicio y lanza las tareas.

        Ejecuta en paralelo (asyncio.gather) el envío de mensajes y, si los comandos están
        habilitados, la recepción de comandos.
        """
        me = None
        try:
            # getMe devuelve los datos del bot; sirve para comprobar que el token es válido.
            me = await self._call("getMe")
            print(f"[telegram] bot @{me['username']} listo")
        except Exception as e:
            print(f"[telegram] token inválido o sin Internet: {e}")
        devs = ", ".join(self.mon.devices)
        # Mensaje de arranque (queda en la cola; se envía cuando sender() empiece a correr).
        await self.send(f"🚀 <b>NetMon iniciado</b>\nMonitoreando: {devs}"
                        + (" <i>(modo simulador)</i>" if self.mon.simulate else ""))
        tasks = [self.sender()]
        if self.cfg.get("commands", True):
            tasks.append(self.poller())
        await asyncio.gather(*tasks)
