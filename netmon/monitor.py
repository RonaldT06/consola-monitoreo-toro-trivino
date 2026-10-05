"""Núcleo: sondeo periódico de equipos + motor de alertas.

Este módulo es el CORAZÓN del sistema. Sus responsabilidades son:

  1. SONDEO: para cada equipo de la configuración ejecuta un bucle infinito que, cada
     "poll_interval" segundos, le pide al driver (drivers.py o sim.py) un "snapshot" del equipo.
  2. ESTADO: guarda en memoria el último estado conocido de cada equipo (DeviceState):
     si responde o no, sus métricas, sus interfaces, su tráfico y un historial corto.
  3. DETECCIÓN Y ALERTAS: compara el snapshot nuevo con el anterior para detectar cambios
     (equipo caído/recuperado, reinicio, interfaz caída/levantada, CPU/memoria alta...) y
     genera "eventos".
  4. DISTRIBUCIÓN: cada evento se guarda en SQLite (store.py) y se entrega a los "listeners"
     suscritos: el bot de Telegram (telegram.py) y el WebSocket del dashboard (web.py).
  5. TRAPS: recibe los traps SNMP que llegan por traps.py y decide qué hacer con ellos.

Máquina de estados de cada equipo (DeviceState.status):

    unknown --(primer sondeo OK)--> up
    unknown/up/snmp_fail --(N fallos seguidos, ping NO responde)--> down  (alerta crítica)
    unknown/up --(N fallos seguidos, ping SÍ responde)--> snmp_fail       (alerta warning)
    down / snmp_fail --(sondeo OK)--> up                              (alerta de recuperación)

  N = general.down_after. Exigir varios fallos seguidos evita falsas alarmas por un solo
  paquete UDP perdido.

Alertas "activas": el diccionario Monitor.active guarda los problemas que siguen abiertos.
La CLAVE identifica el problema y permite cerrarlo después:
    "<equipo>:reach"       -> equipo caído o sin SNMP
    "<equipo>:if:<ifIndex>" -> interfaz caída
    "<equipo>:cpu" / ":mem" / ":temp" / ":disk" -> umbral superado
Un evento con key=... ABRE una alerta; un evento con clear=... la CIERRA.

Concurrencia (asyncio): cada equipo tiene su propia tarea; mientras un equipo tarda en
responder (espera de red), los demás siguen siendo consultados. Todo corre en un solo hilo.
"""
from __future__ import annotations

import asyncio
import collections
import html
import platform
import time
from typing import Awaitable, Callable

from .drivers import make_driver
from .sim import SimDriver
from .store import EventStore

# Orden numérico de las severidades: permite comparar ("warning" >= "info") para filtrar,
# por ejemplo, qué eventos se envían a Telegram (min_severity).
SEV_ORDER = {"info": 0, "warning": 1, "critical": 2}

# Traps conocidos: OID del trap -> (nombre legible, severidad).
# Los 1.3.6.1.6.3.1.1.5.x son los traps genéricos estándar (SNMPv2-MIB); los
# 1.3.6.1.4.1.12356... son traps propios de FortiGate.
TRAP_NAMES = {
    "1.3.6.1.6.3.1.1.5.1": ("coldStart", "warning"),        # el equipo arrancó (encendido)
    "1.3.6.1.6.3.1.1.5.2": ("warmStart", "warning"),        # el agente SNMP se reinició
    "1.3.6.1.6.3.1.1.5.3": ("linkDown", "critical"),        # una interfaz perdió enlace
    "1.3.6.1.6.3.1.1.5.4": ("linkUp", "info"),              # una interfaz recuperó enlace
    "1.3.6.1.6.3.1.1.5.5": ("authenticationFailure", "warning"),  # alguien usó una community mala
    "1.3.6.1.4.1.12356.101.2.0.101": ("FortiGate CPU alta", "warning"),
    "1.3.6.1.4.1.12356.101.2.0.102": ("FortiGate memoria alta", "warning"),
    "1.3.6.1.4.1.12356.101.2.0.103": ("FortiGate disco de logs lleno", "warning"),
    "1.3.6.1.4.1.12356.101.2.0.301": ("FortiGate VPN túnel UP", "info"),
    "1.3.6.1.4.1.12356.101.2.0.302": ("FortiGate VPN túnel DOWN", "critical"),
    "1.3.6.1.4.1.12356.101.2.0.401": ("FortiGate cambio HA", "warning"),
}
# Traps que solo disparan un sondeo inmediato (la alerta la genera el sondeo, así no se duplica)
# Explicación: el sondeo periódico YA detecta reinicios e interfaces caídas/levantadas. Si
# además se alertara por el trap, el usuario recibiría DOS mensajes del mismo hecho. Así que el
# trap solo "despierta" al sondeo antes de tiempo: la detección es casi instantánea (no hay que
# esperar los 10 s del intervalo) y la alerta sale una sola vez, con el mismo formato de siempre.
TRAPS_REPOLL = {"1.3.6.1.6.3.1.1.5.1", "1.3.6.1.6.3.1.1.5.2", "1.3.6.1.6.3.1.1.5.3", "1.3.6.1.6.3.1.1.5.4"}


def fmt_duration(sec: float) -> str:
    """Convierte una cantidad de segundos en texto legible, ej. 3725 -> "1h 2m".

    Parámetros:
        sec: duración en segundos (si es negativa se toma como 0).

    Retorna:
        Texto con las dos unidades más significativas (días/horas/minutos/segundos).
    """
    sec = int(max(sec, 0))
    # divmod devuelve (cociente, residuo): se van sacando días, horas y minutos.
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


async def ping_host(host: str, timeout: float = 1.0) -> bool:
    """Hace un ping ICMP (un solo paquete) usando el comando "ping" del sistema operativo.

    Se usa para diagnosticar cuando SNMP falla: si el ping responde, el equipo está vivo y el
    problema es solo de SNMP (credenciales, ACL...); si no responde, el equipo está caído o
    sin conectividad.

    Parámetros:
        host:    IP o nombre del equipo.
        timeout: segundos a esperar la respuesta.

    Retorna:
        True si hubo respuesta, False en caso contrario (o si ocurrió cualquier error).
    """
    # Las opciones del comando ping cambian según el sistema operativo:
    #   Windows: -n = cantidad de paquetes, -w = timeout en milisegundos.
    #   macOS:   -c = cantidad, -t = timeout total en segundos.
    #   Linux:   -c = cantidad, -W = timeout en segundos.
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    elif system == "darwin":
        cmd = ["ping", "-c", "1", "-t", str(max(1, int(timeout))), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        # Se lanza como subproceso ASÍNCRONO: mientras se espera el ping, el bucle de asyncio
        # sigue atendiendo a otros equipos y a la web (no se bloquea el programa).
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        # wait_for pone un límite de tiempo por si el comando ping se queda colgado.
        out, _ = await asyncio.wait_for(proc.communicate(), timeout + 2)
        # en Windows el código de salida no es fiable ("host inaccesible" devuelve 0)
        # Por eso se busca "ttl=" en la salida: solo aparece en una respuesta de eco real.
        return "ttl=" in out.decode(errors="ignore").lower()
    except Exception:
        return False


class DeviceState:
    """Estado en memoria de UN equipo monitoreado.

    Guarda la configuración, el driver, el estado actual (máquina de estados), el último
    snapshot, las interfaces con su tráfico calculado, contadores para los umbrales y el
    historial para las gráficas del dashboard.
    """

    def __init__(self, cfg: dict, general: dict, simulate: bool):
        """Inicializa el estado del equipo.

        Parámetros:
            cfg:      configuración de este equipo (un elemento de cfg["devices"]).
            general:  sección "general" de la configuración.
            simulate: si es True se usa un equipo virtual (SimDriver) en vez de SNMP real.
        """
        self.cfg = cfg
        self.name = cfg["name"]
        # Ambos drivers ofrecen el mismo método poll(), así que el resto del código no
        # necesita saber si el equipo es real o simulado.
        self.driver = SimDriver(cfg, general) if simulate else make_driver(cfg, general)
        self.status = "unknown"          # unknown | up | down | snmp_fail
        self.fails = 0                   # sondeos fallidos consecutivos
        self.down_since: float | None = None   # momento (epoch) en que se declaró caído
        self.last_ok: float | None = None      # último sondeo exitoso
        self.last_poll: float | None = None    # último intento de sondeo (exitoso o no)
        self.last_error: str | None = None     # texto del último error SNMP
        self.poll_ms: float | None = None      # duración del último sondeo, en milisegundos
        self.snap: dict = {}                   # último snapshot recibido del driver
        self.ifaces: dict = {}           # ifIndex -> dict + in_bps/out_bps
        self.prev_octets: dict = {}      # ifIndex -> (t, in, out)
        # Cuenta cuántos sondeos SEGUIDOS lleva cada métrica (cpu, mem...) sobre su umbral.
        # Counter devuelve 0 para claves que aún no existen.
        self.over: collections.Counter = collections.Counter()
        # deque con tamaño máximo: al llenarse, los puntos más viejos se descartan solos
        # (buffer circular). Alimenta las gráficas del dashboard.
        self.history = collections.deque(maxlen=general["history_points"])
        # Event de asyncio: es una "bandera" que una tarea puede esperar (wait) y otra puede
        # activar (set). Se usa para DESPERTAR el bucle de este equipo antes de que termine
        # su intervalo (por un trap, un botón del dashboard o el simulador).
        self.wake = asyncio.Event()

    def to_dict(self) -> dict:
        """Convierte el estado del equipo en un diccionario serializable a JSON.

        Es lo que reciben el dashboard (API /api/state y WebSocket).

        Retorna:
            Diccionario con identificación, estado, métricas, umbrales, interfaces e historial.
        """
        s = self.snap
        return {
            "name": self.name,
            "host": self.cfg["host"],
            "vendor": self.cfg["vendor"],
            "role": self.cfg.get("role"),
            "status": self.status,
            "last_ok": self.last_ok,
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "down_since": self.down_since,
            "poll_ms": self.poll_ms,
            "sys_name": s.get("sys_name"),
            "sys_descr": s.get("sys_descr"),
            "version": s.get("version"),
            "uptime": s.get("uptime"),
            "cpu": s.get("cpu"),
            "mem": s.get("mem"),
            "temp": s.get("temp"),
            "sessions": s.get("sessions"),
            "disk": s.get("disk"),
            "thresholds": self.cfg["thresholds"],
            # Interfaces como lista ordenada por ifIndex NUMÉRICO (si se ordenara como texto,
            # "10" quedaría antes que "2"). Los índices no numéricos se ponen al inicio (0).
            "interfaces": [dict(idx=k, **v) for k, v in sorted(self.ifaces.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)],
            "history": list(self.history),
        }


# Tipo de las funciones "listener" de eventos: reciben el evento (dict) y son asíncronas.
Listener = Callable[[dict], Awaitable[None]]


class Monitor:
    """Coordina el sondeo de todos los equipos, el motor de alertas y la difusión de eventos."""

    def __init__(self, cfg: dict, simulate: bool = False):
        """Crea el estado de cada equipo y abre la base de datos de eventos.

        Parámetros:
            cfg:      configuración completa ya validada (config.load_config).
            simulate: True para usar equipos virtuales.
        """
        self.cfg = cfg
        self.general = cfg["general"]
        self.simulate = simulate
        # Diccionario nombre -> DeviceState.
        self.devices = {d["name"]: DeviceState(d, self.general, simulate) for d in cfg["devices"]}
        # Diccionario IP -> nombre: permite saber qué equipo envió un trap (por su IP de origen).
        self.by_host = {d["host"]: d["name"] for d in cfg["devices"]}
        self.store = EventStore(self.general["db_path"])
        self.active: dict[str, dict] = {}   # alertas activas: clave -> evento
        # Funciones a llamar cuando hay un EVENTO nuevo (Telegram, WebSocket).
        self.event_listeners: list[Listener] = []
        # Funciones a llamar cuando cambia el ESTADO (tras cada sondeo) -> refresco del dashboard.
        self.state_listeners: list[Callable[[], Awaitable[None]]] = []
        self.started = time.time()

    # ------------------------------------------------------------ eventos
    async def emit(self, device: str, severity: str, kind: str, message: str,
                   key: str | None = None, clear: str | None = None) -> dict:
        """Crea un evento, actualiza las alertas activas, lo guarda y lo reparte a los listeners.

        Parámetros:
            device:   nombre del equipo (o IP si el trap viene de un equipo desconocido).
            severity: "info", "warning" o "critical".
            kind:     tipo de evento (ej. "device_down", "if_up", "cpu_high", "trap").
            message:  texto en HTML simple (<b>, <i>) que se muestra en Telegram y el dashboard.
            key:      si se indica, ABRE (o actualiza) una alerta activa con esa clave.
            clear:    si se indica, CIERRA la alerta activa con esa clave.

        Retorna:
            El evento creado (dict), con su "id" asignado por la base de datos.
        """
        ev = {"ts": time.time(), "device": device, "severity": severity, "kind": kind, "message": message}
        if key:
            self.active[key] = ev
        if clear:
            # pop(..., None) no falla si la alerta ya no existía.
            self.active.pop(clear, None)
        ev["id"] = self.store.add(ev)
        print(f"[{time.strftime('%H:%M:%S')}] {severity.upper():8} {device}: {message}")
        # Se avisa a cada suscriptor. Cada uno va en su propio try para que un error en uno
        # (por ejemplo, Telegram sin Internet) no impida avisar a los demás.
        for fn in self.event_listeners:
            try:
                await fn(ev)
            except Exception as e:  # un listener caído no debe tumbar el monitor
                print(f"[monitor] listener error: {e}")
        return ev

    async def _notify_state(self):
        """Avisa a los listeners de estado (el dashboard) que hay datos nuevos para mostrar."""
        for fn in self.state_listeners:
            try:
                await fn()
            except Exception as e:
                print(f"[monitor] state listener error: {e}")

    # ------------------------------------------------------------ bucle
    async def run(self):
        """Arranca un bucle de sondeo por cada equipo y los ejecuta todos en paralelo.

        asyncio.gather lanza las corrutinas como tareas concurrentes: los equipos se consultan
        de forma independiente, y uno lento o caído no retrasa a los demás.
        """
        await asyncio.gather(*(self._loop(ds) for ds in self.devices.values()))

    async def _loop(self, ds: DeviceState):
        """Bucle infinito de un equipo: sondear, notificar y esperar el siguiente turno.

        Parámetros:
            ds: estado del equipo a sondear.
        """
        interval = self.general["poll_interval"]
        while True:
            try:
                await self.poll_once(ds)
            except Exception as e:
                # Red de seguridad: un error de programación no debe detener el bucle del equipo.
                print(f"[monitor] error inesperado en {ds.name}: {e!r}")
            await self._notify_state()
            # Se "baja la bandera" antes de esperar, para que solo cuenten los avisos nuevos.
            ds.wake.clear()
            try:
                # Espera hasta que pase el intervalo O hasta que alguien llame a wake.set()
                # (poll_now). Lo que ocurra primero. Así se puede forzar un sondeo inmediato
                # sin esperar los segundos restantes.
                await asyncio.wait_for(ds.wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                # Caso normal: se cumplió el intervalo sin que nadie despertara al equipo.
                pass

    def poll_now(self, name: str):
        """Pide un sondeo inmediato de un equipo (activa su Event "wake").

        Lo usan los traps, los botones del dashboard y los controles del simulador.

        Parámetros:
            name: nombre del equipo; si no existe, no hace nada.
        """
        if name in self.devices:
            self.devices[name].wake.set()

    async def _ping(self, ds: DeviceState) -> bool:
        """Hace ping al equipo; en modo simulador pregunta al equipo virtual si está "encendido".

        Parámetros:
            ds: estado del equipo.

        Retorna:
            True si el equipo responde al ping.
        """
        if isinstance(ds.driver, SimDriver):
            return await ds.driver.ping()
        return await ping_host(ds.cfg["host"])

    async def poll_once(self, ds: DeviceState):
        """Realiza UN sondeo del equipo y delega en _on_success o _on_fail según el resultado.

        Parámetros:
            ds: estado del equipo.
        """
        t0 = time.time()
        ds.last_poll = t0
        try:
            snap = await ds.driver.poll()
        except Exception as e:
            # Cualquier error (timeout SNMP, credenciales...) cuenta como sondeo fallido.
            # Si el error no trae mensaje se guarda el nombre de su clase (ej. "TimeoutError").
            ds.last_error = str(e) or e.__class__.__name__
            await self._on_fail(ds)
            return
        # Tiempo que tardó el sondeo completo, útil para ver la latencia del equipo.
        ds.poll_ms = round((time.time() - t0) * 1000)
        ds.last_error = None
        await self._on_success(ds, snap, t0)

    # ------------------------------------------------------------ fallos
    async def _on_fail(self, ds: DeviceState):
        """Procesa un sondeo fallido: decide si el equipo pasa a "down" o a "snmp_fail".

        Parámetros:
            ds: estado del equipo.
        """
        ds.fails += 1
        # Todavía no se alcanzan los fallos necesarios (puede ser un paquete perdido), o el
        # equipo ya estaba marcado como caído (no se repite la alerta).
        if ds.fails < self.general["down_after"] or ds.status == "down":
            return
        # Diagnóstico con ping (si está habilitado para este equipo).
        ping_ok = await self._ping(ds) if ds.cfg.get("ping", True) else False
        prev = ds.status
        if ping_ok and prev != "snmp_fail":
            # Responde ping pero no SNMP: el equipo está vivo; el problema es de configuración
            # SNMP (community/usuario incorrecto, ACL, política del firewall...).
            ds.status = "snmp_fail"
            await self.emit(ds.name, "warning", "snmp_fail",
                            f"⚠️ <b>{ds.name}</b> responde ping pero NO responde SNMP "
                            f"(revisar community/usuario, ACL o política de acceso). Error: {html.escape(ds.last_error or '')}",
                            key=f"{ds.name}:reach")
        elif not ping_ok:
            # No responde ni SNMP ni ping: se considera caído.
            ds.status = "down"
            ds.down_since = time.time()
            # Se borran las tasas de tráfico para que el dashboard no muestre valores viejos.
            for i in ds.ifaces.values():
                i["in_bps"] = i["out_bps"] = None
            # Se usa la MISMA clave ":reach" que snmp_fail: el estado de alcanzabilidad es un
            # único problema, así que la alerta nueva reemplaza a la anterior.
            await self.emit(ds.name, "critical", "device_down",
                            f"🔴 <b>{ds.name}</b> ({ds.cfg['host']}) NO RESPONDE — apagado, reiniciando o sin conectividad",
                            key=f"{ds.name}:reach")

    # ------------------------------------------------------------ éxito
    async def _on_success(self, ds: DeviceState, snap: dict, now: float):
        """Procesa un sondeo exitoso: transiciones de estado, interfaces, umbrales e historial.

        Parámetros:
            ds:   estado del equipo.
            snap: snapshot recién obtenido del driver.
            now:  momento (epoch, segundos) en que empezó el sondeo.
        """
        prev_status, prev_snap = ds.status, ds.snap
        ds.fails = 0
        ds.last_ok = now

        if prev_status == "down":
            # Recuperación tras estar caído.
            downtime = now - (ds.down_since or now)
            extra = ""
            # Si el uptime del equipo es menor (o casi igual) que el tiempo que estuvo caído,
            # significa que el equipo arrancó de nuevo durante la caída: fue un REINICIO y no
            # solo un corte de red. Se da un margen de 2 intervalos de sondeo.
            if snap.get("uptime") is not None and snap["uptime"] <= downtime + self.general["poll_interval"] * 2:
                extra = f" — el equipo se REINICIÓ (uptime {fmt_duration(snap['uptime'])})"
            await self.emit(ds.name, "info", "device_up",
                            f"🟢 <b>{ds.name}</b> volvió a responder tras {fmt_duration(downtime)}{extra}",
                            clear=f"{ds.name}:reach")
            ds.down_since = None
        elif prev_status == "snmp_fail":
            await self.emit(ds.name, "info", "snmp_ok", f"🟢 <b>{ds.name}</b> SNMP restablecido",
                            clear=f"{ds.name}:reach")
        elif prev_status == "unknown":
            # Primer contacto exitoso desde que arrancó el programa.
            await self.emit(ds.name, "info", "connected",
                            f"🔗 <b>{ds.name}</b> conectado ({ds.cfg['host']}) — "
                            f"{len(snap.get('interfaces', {}))} interfaces monitoreadas")
        # Reinicio detectado SIN que el equipo llegara a marcarse como caído (reinicio rápido
        # entre dos sondeos): el uptime "retrocedió". Se usa un margen de 5 s para tolerar
        # pequeñas diferencias de redondeo.
        elif prev_status == "up" and prev_snap.get("uptime") and snap.get("uptime") is not None \
                and snap["uptime"] + 5 < prev_snap["uptime"]:
            await self.emit(ds.name, "warning", "reboot",
                            f"🔄 <b>{ds.name}</b> se reinició (uptime {fmt_duration(snap['uptime'])})")

        ds.status = "up"
        # first=True en el primer sondeo: no hay estado anterior con qué comparar, así que no se
        # generan alertas de interfaces (evita "alertas" por interfaces que siempre estuvieron abajo).
        await self._check_interfaces(ds, snap, now, first=prev_status == "unknown")
        await self._check_thresholds(ds, snap)
        ds.snap = snap

        # Tráfico total del equipo = suma de todas sus interfaces (para la gráfica de historial).
        tot_in = sum(i.get("in_bps") or 0 for i in ds.ifaces.values())
        tot_out = sum(i.get("out_bps") or 0 for i in ds.ifaces.values())
        ds.history.append({"t": round(now), "cpu": snap.get("cpu"), "mem": snap.get("mem"),
                           "in": round(tot_in), "out": round(tot_out)})

    async def _check_interfaces(self, ds: DeviceState, snap: dict, now: float, first: bool):
        """Calcula el tráfico (bps) de cada interfaz y genera alertas por cambios de estado.

        Cálculo de tráfico: SNMP no entrega "bits por segundo"; entrega CONTADORES que acumulan
        los bytes desde que arrancó el equipo. La tasa se obtiene comparando dos lecturas:

            bps = (octetos_ahora - octetos_antes) * 8 / (t_ahora - t_antes)

        (se multiplica por 8 porque 1 octeto = 8 bits).

        Parámetros:
            ds:    estado del equipo.
            snap:  snapshot nuevo.
            now:   momento del sondeo actual.
            first: True si es el primer sondeo exitoso (no se alerta, solo se toma referencia).
        """
        new = {}
        for idx, i in snap.get("interfaces", {}).items():
            # Copia para no modificar el diccionario que entregó el driver.
            i = dict(i)
            # --- tasas de tráfico
            i["in_bps"] = i["out_bps"] = None
            # Lectura anterior de esta interfaz: (tiempo, octetos_entrada, octetos_salida).
            prev = ds.prev_octets.get(idx)
            if prev and i.get("in_octets") is not None and prev[1] is not None:
                dt = now - prev[0]
                din, dout = i["in_octets"] - prev[1], (i.get("out_octets") or 0) - (prev[2] or 0)
                # Si la diferencia es negativa, el contador volvió a empezar: o el equipo se
                # reinició, o un contador de 32 bits se desbordó ("counter wrap"). En ese caso
                # la resta no tiene sentido y se descarta esta medición (queda None una vez).
                if dt > 0 and din >= 0 and dout >= 0:  # descarta reinicio/desborde de contador
                    i["in_bps"], i["out_bps"] = din * 8 / dt, dout * 8 / dt
            # Se guarda la lectura actual como referencia para el próximo sondeo.
            ds.prev_octets[idx] = (now, i.get("in_octets"), i.get("out_octets"))
            new[idx] = i

            # --- detección de cambios de estado de la interfaz
            old = ds.ifaces.get(idx)
            # Sin estado anterior (primer sondeo o interfaz nueva) no hay cambio que reportar.
            if first or not old:
                continue
            label = f"<b>{ds.name}</b> interfaz <b>{html.escape(i['name'])}</b>" + (f" ({html.escape(i['alias'])})" if i.get("alias") else "")
            # Clave de alerta activa para esta interfaz.
            key = f"{ds.name}:if:{idx}"
            if old["oper"] == "up" and i["oper"] != "up":
                # La interfaz perdió el enlace. Se mira ifAdminStatus para saber POR QUÉ:
                if i["admin"] == "down":
                    # admin=down: alguien hizo "shutdown" a propósito -> warning.
                    await self.emit(ds.name, "warning", "if_admin_down",
                                    f"🟠 {label} DESHABILITADA (shutdown administrativo)", key=key)
                else:
                    # admin=up pero oper=down: estaba habilitada y se cayó -> falla real, crítico.
                    await self.emit(ds.name, "critical", "if_down",
                                    f"🔴 {label} CAÍDA (link down — cable desconectado o extremo apagado)", key=key)
            elif old["oper"] != "up" and i["oper"] == "up":
                # Enlace recuperado: se cierra la alerta de esta interfaz.
                await self.emit(ds.name, "info", "if_up", f"🟢 {label} ARRIBA (link up)", clear=key)
            elif old["admin"] == "up" and i["admin"] == "down" and old["oper"] != "up":
                # Se hizo shutdown a una interfaz que ya no tenía enlace: solo informativo
                # (no abre alerta porque no se perdió servicio en este momento).
                await self.emit(ds.name, "info", "if_admin_down",
                                f"⚪ {label} deshabilitada (ya estaba sin enlace)")
        ds.ifaces = new

    async def _check_thresholds(self, ds: DeviceState, snap: dict):
        """Revisa CPU, memoria, temperatura y disco contra sus umbrales, con histéresis.

        Dos mecanismos evitan alertas repetidas o "parpadeantes":
          - sustain: la métrica debe estar sobre el umbral durante "sustain" sondeos SEGUIDOS
            antes de alertar (un pico de un solo sondeo no genera alerta).
          - margen de -5: una vez abierta la alerta, solo se cierra cuando el valor baja a
            menos de (umbral - 5). Si el valor oscila alrededor del umbral (79, 81, 79, 81...)
            no se envían alertas de "alta" y "normalizada" una tras otra. Esto se llama
            HISTÉRESIS.

        Parámetros:
            ds:   estado del equipo.
            snap: snapshot nuevo.
        """
        th = ds.cfg["thresholds"]
        # (campo del snapshot, nombre para el mensaje, unidad, umbral)
        checks = [
            ("cpu", "CPU", "%", th["cpu_high"]),
            ("mem", "Memoria", "%", th["mem_high"]),
            ("temp", "Temperatura", "°C", th["temp_high"]),
            ("disk", "Disco", "%", th["disk_high"]),
        ]
        for field, label, unit, limit in checks:
            val = snap.get(field)
            key = f"{ds.name}:{field}"
            # El equipo no reporta esta métrica (ej. un FortiGate no da temperatura): se omite.
            if val is None:
                continue
            if val >= limit:
                ds.over[field] += 1
                # Se alerta EXACTAMENTE cuando el contador llega a "sustain" (no en cada sondeo
                # posterior) y solo si la alerta no está ya activa.
                if ds.over[field] == th["sustain"] and key not in self.active:
                    await self.emit(ds.name, "warning", f"{field}_high",
                                    f"📈 <b>{ds.name}</b> {label} alta: {val:.0f}{unit} (umbral {limit}{unit})", key=key)
            else:
                # Bajo el umbral: se reinicia la cuenta de sondeos seguidos.
                ds.over[field] = 0
                # Cierre con margen de histéresis (5 unidades por debajo del umbral).
                if key in self.active and val < limit - 5:
                    await self.emit(ds.name, "info", f"{field}_ok",
                                    f"✅ <b>{ds.name}</b> {label} normalizada: {val:.0f}{unit}", clear=key)

    # ------------------------------------------------------------ traps
    async def on_trap(self, src_ip: str, trap_oid: str, varbinds: list[tuple[str, str]]):
        """Procesa un trap SNMP recibido por traps.py.

        Un TRAP es un mensaje que el equipo envía POR INICIATIVA PROPIA (a UDP 162) cuando
        ocurre algo, sin esperar a que se le pregunte. Es el complemento del sondeo periódico.

        Parámetros:
            src_ip:   IP de origen del paquete (identifica al equipo).
            trap_oid: OID que identifica el tipo de trap (snmpTrapOID.0).
            varbinds: lista de pares (OID, valor en texto) que vienen dentro del trap.
        """
        # Si la IP no corresponde a ningún equipo configurado, se usa la IP como nombre.
        device = self.by_host.get(src_ip, src_ip)
        if trap_oid in TRAPS_REPOLL:
            # linkDown/linkUp/coldStart/warmStart: solo se fuerza un sondeo inmediato. La alerta
            # la generará el sondeo (ver comentario de TRAPS_REPOLL), evitando duplicados.
            if device in self.devices:
                self.poll_now(device)
            else:
                # IP de origen desconocida (p. ej. traps que llegan a través del NAT de Docker):
                # se consultan todos los equipos para detectar el cambio de inmediato
                for name in self.devices:
                    self.poll_now(name)
            return
        # Para los demás traps sí se genera un evento. Si el OID no es conocido se muestra
        # el OID tal cual con severidad "info".
        name, sev = TRAP_NAMES.get(trap_oid, (trap_oid, "info"))
        # Los dos primeros varbinds de un trap v2c son siempre sysUpTime.0 y snmpTrapOID.0;
        # la información útil viene después. Se toman hasta 3 valores, se escapan para HTML
        # y se limita el texto a 300 caracteres.
        detail = html.escape("; ".join(f"{v}" for o, v in varbinds[2:5] if v)[:300])
        await self.emit(device, sev, "trap", f"📨 Trap de <b>{device}</b>: {name}" + (f" — {detail}" if detail else ""))
        # Además se refresca el estado del equipo para que el dashboard muestre datos al día.
        if device in self.devices:
            self.poll_now(device)

    # ------------------------------------------------------------ vistas
    def state(self) -> dict:
        """Devuelve una "foto" completa del sistema para el dashboard (JSON).

        Retorna:
            Diccionario con hora actual, hora de inicio, modo simulador, intervalo de sondeo,
            la lista de equipos (DeviceState.to_dict) y las alertas activas, de la más reciente
            a la más antigua.
        """
        return {
            "ts": time.time(),
            "started": self.started,
            "simulate": self.simulate,
            "poll_interval": self.general["poll_interval"],
            "devices": [ds.to_dict() for ds in self.devices.values()],
            "active": sorted(self.active.values(), key=lambda e: -e["ts"]),
        }
