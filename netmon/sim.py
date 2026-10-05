"""Modo simulador: equipos virtuales para probar dashboard y Telegram sin PNETLab.

Desde el dashboard (modo --simulate) puedes: apagar/encender equipos,
hacer shutdown / quitar cable de interfaces y provocar picos de CPU/memoria.

Rol de este módulo en el sistema:
  SimDriver REEMPLAZA a los drivers SNMP reales (drivers.py) cuando el programa se ejecuta con
  --simulate. Ofrece los mismos métodos (poll() devuelve un snapshot con el mismo formato), por
  lo que el monitor, las alertas, Telegram y el dashboard funcionan exactamente igual que con
  equipos reales. Así se puede probar y demostrar todo el sistema sin laboratorio ni red.

  - Recibe: la configuración del equipo (nombre, vendor, role).
  - Produce: snapshots con valores inventados pero realistas (CPU, memoria, tráfico...).
  - Lo controla: web.py (rutas /api/sim/...) llamando a toggle_power, toggle_admin,
    toggle_link y spike.
"""
from __future__ import annotations

import random
import time

from .snmp import SnmpError

# Nombres de interfaces típicos de cada tipo de equipo simulado.
PORTS = {
    "fortigate": ["port1", "port2", "port3", "port4"],
    "huawei-switch": [f"GE1/0/{i}" for i in range(8)],
    "huawei": [f"GigabitEthernet0/0/{i}" for i in range(4)],
    "generic": ["eth0", "eth1"],
}
# Textos sysDescr realistas para cada tipo de equipo simulado.
DESCR = {
    "fortigate": "FortiGate-VM64-KVM v7.4.4,build2662",
    "huawei-switch": "Huawei Versatile Routing Platform Software VRP (R) software, Version 8.180 (CE12800 V200R005C10SPC607B607)",
    "huawei": "Huawei Versatile Routing Platform Software VRP (R) software, Version 5.160 (AR1000V V300R019C00SPC300)",
    "generic": "Generic device",
}


class SimDriver:
    """Equipo virtual que imita el comportamiento de un driver SNMP real."""

    def __init__(self, dev: dict, general: dict):
        """Crea el equipo virtual con valores iniciales aleatorios.

        Parámetros:
            dev:     configuración del equipo (name, vendor, role).
            general: sección "general" (no se usa; se recibe para tener la misma firma que
                     los drivers reales).
        """
        self.dev = dev
        kind = dev["vendor"]
        # Un Huawei cuyo "role" contiene "switch" se simula como switch (más puertos).
        if kind == "huawei" and "switch" in dev.get("role", "").lower():
            kind = "huawei-switch"
        self.kind = kind
        self.powered = True
        # Momento de "arranque" simulado: entre 1 hora y 5 días atrás (para que el uptime
        # inicial sea creíble).
        self.boot = time.time() - random.randint(3600, 86400 * 5)
        self.cpu = random.uniform(8, 25)
        self.mem = random.uniform(30, 55)
        # Hasta qué momento dura un pico de CPU/memoria provocado (0 = sin pico).
        self.spike_until = 0.0
        self.ifaces = {}
        # Se crean las interfaces con ifIndex "1", "2", ... (texto, como los entrega SNMP).
        for i, n in enumerate(PORTS[kind], start=1):
            self.ifaces[str(i)] = {
                "name": n, "alias": "", "admin": "up",
                # Aproximadamente la mitad de los puertos (mínimo 2) arrancan con enlace.
                "oper": "up" if i <= max(2, len(PORTS[kind]) // 2) else "down",
                # Contadores de bytes iniciales aleatorios (como si el equipo llevara tiempo).
                "in_octets": random.randint(10**6, 10**9), "out_octets": random.randint(10**6, 10**9),
                # "_rate": tasa de tráfico interna en bps (0,2 a 12 Mbps). El "_" indica que es
                # un dato privado del simulador que no se incluye en el snapshot.
                "speed_mbps": 1000, "_rate": random.uniform(0.2e6, 12e6),
            }

    # --- acciones desde el dashboard ---
    def toggle_power(self):
        """Apaga o enciende el equipo virtual.

        Retorna:
            True si quedó encendido, False si quedó apagado.
        """
        self.powered = not self.powered
        if self.powered:
            # Al encender, el uptime vuelve a empezar desde 0 (igual que un equipo real), lo
            # que permite al monitor detectar que hubo un REINICIO.
            self.boot = time.time()
        return self.powered

    def toggle_admin(self, idx: str):
        """Simula "shutdown" / "no shutdown" en una interfaz.

        Con shutdown, el estado administrativo y el operativo pasan a "down"; al habilitarla,
        ambos vuelven a "up".

        Parámetros:
            idx: ifIndex de la interfaz.
        """
        i = self.ifaces[idx]
        if i["admin"] == "up":
            i["admin"], i["oper"] = "down", "down"
        else:
            i["admin"], i["oper"] = "up", "up"

    def toggle_link(self, idx: str):
        """Simula desconectar / conectar el cable de una interfaz.

        Solo cambia el estado operativo (oper), y solo si la interfaz está habilitada
        (admin=up): una interfaz en shutdown no puede tener enlace.

        Parámetros:
            idx: ifIndex de la interfaz.
        """
        i = self.ifaces[idx]
        if i["admin"] == "up":
            i["oper"] = "down" if i["oper"] == "up" else "up"

    def spike(self, seconds=60):
        """Provoca un pico de CPU y memoria durante cierto tiempo.

        Parámetros:
            seconds: duración del pico en segundos.
        """
        self.spike_until = time.time() + seconds

    async def ping(self) -> bool:
        """Simula un ping: responde solo si el equipo está encendido.

        Retorna:
            True si está encendido.
        """
        return self.powered

    async def poll(self) -> dict:
        """Genera un snapshot con el mismo formato que los drivers reales.

        Si el equipo está "apagado" lanza SnmpError, igual que haría un equipo real que no
        responde, para que el monitor ejecute su lógica normal de fallos.

        Retorna:
            Diccionario snapshot (sys_name, sys_descr, uptime, cpu, mem, temp, sessions, disk,
            version, interfaces).
        """
        if not self.powered:
            raise SnmpError("No SNMP response received before timeout")
        spiking = time.time() < self.spike_until
        # Valor "objetivo" hacia donde tienden CPU y memoria (alto durante un pico).
        target_cpu = 95 if spiking else 15
        target_mem = 92 if spiking else 45
        # CAMINATA ALEATORIA con tendencia ("random walk"): en cada sondeo el valor se acerca
        # una fracción al objetivo (50 % en CPU, 30 % en memoria) y se le suma un pequeño ruido
        # aleatorio. Así las gráficas se ven naturales: varían, pero sin saltos absurdos.
        self.cpu += (target_cpu - self.cpu) * 0.5 + random.uniform(-3, 3)
        self.mem += (target_mem - self.mem) * 0.3 + random.uniform(-1, 1)
        # Se mantienen los valores dentro de rangos posibles (porcentajes válidos).
        self.cpu = min(max(self.cpu, 1), 100)
        self.mem = min(max(self.mem, 5), 100)
        ifs = {}
        for idx, i in self.ifaces.items():
            if i["oper"] == "up":
                # La tasa de tráfico también hace una caminata aleatoria: se multiplica por un
                # factor entre 0,8 y 1,25 (con un mínimo de 0,05 Mbps).
                i["_rate"] = max(0.05e6, i["_rate"] * random.uniform(0.8, 1.25))
                # Se aumentan los contadores de octetos como si pasaran 10 segundos de tráfico:
                # bits/s / 8 = bytes/s, por 10 s. La salida es una fracción (30-90 %) de la entrada.
                # Luego el monitor calcula los bps a partir de estas diferencias, igual que con
                # equipos reales.
                i["in_octets"] += int(i["_rate"] / 8 * 10)
                i["out_octets"] += int(i["_rate"] / 8 * 10 * random.uniform(0.3, 0.9))
            # Copia de la interfaz SIN las claves internas que empiezan con "_".
            ifs[idx] = {k: v for k, v in i.items() if not k.startswith("_")}
        snap = {
            "sys_name": self.dev["name"], "sys_descr": DESCR[self.kind],
            "uptime": int(time.time() - self.boot),
            "cpu": round(self.cpu, 1), "mem": round(self.mem, 1),
            # Solo los Huawei reportan temperatura; se simula proporcional a la carga de CPU.
            "temp": round(40 + self.cpu * 0.3, 1) if self.kind.startswith("huawei") else None,
            # Sesiones, disco y versión solo existen en el FortiGate (igual que en el driver real).
            "sessions": random.randint(800, 2500) if self.kind == "fortigate" else None,
            "disk": 12.4 if self.kind == "fortigate" else None,
            "version": "v7.4.4" if self.kind == "fortigate" else None,
            "interfaces": ifs,
        }
        return snap
