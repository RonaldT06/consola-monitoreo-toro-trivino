"""Drivers por fabricante. Cada driver devuelve un 'snapshot' normalizado:

{
  "sys_name": str, "sys_descr": str, "uptime": int (segundos),
  "cpu": float|None, "mem": float|None, "temp": float|None,
  "sessions": int|None, "disk": float|None, "version": str|None,
  "interfaces": {ifIndex: {"name","alias","admin","oper","in_octets","out_octets","speed_mbps"}}
}

Para agregar otro fabricante (Cisco, MikroTik, pfSense...) basta con crear
una clase que herede de BaseDriver y sobreescribir vendor_metrics().

Rol de este módulo en el sistema:
  Cada fabricante guarda datos como CPU, memoria o temperatura en OIDs DIFERENTES (en su MIB
  privada). Los "drivers" esconden esas diferencias: el monitor (monitor.py) siempre recibe el
  mismo formato de "snapshot" (foto del estado del equipo en un instante), sin importar si el
  equipo es FortiGate, Huawei o genérico.

  - Recibe: la configuración del equipo y la sección "general" (timeouts/reintentos SNMP).
  - Produce: el diccionario "snapshot" descrito arriba, en cada llamada a poll().
  - Usa: snmp.py (SnmpClient) para hacer los GET y WALK.

Lo estándar (sistema e interfaces, MIB-II / IF-MIB) se resuelve en BaseDriver y lo heredan
todos; lo propio de cada marca se resuelve en vendor_metrics().
"""
from __future__ import annotations

import re

from .snmp import SnmpClient

# ---- MIB-II / IF-MIB (estándar, lo soporta cualquier equipo) ----
# Los OIDs que terminan en ".0" son ESCALARES (un único valor): se leen con GET.
# Los demás son COLUMNAS de tablas (un valor por interfaz/fila): se leen con WALK.
SYS_DESCR = "1.3.6.1.2.1.1.1.0"     # sysDescr: descripción del equipo (modelo, sistema operativo)
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"    # sysUpTime: tiempo encendido, en CENTÉSIMAS de segundo
SYS_NAME = "1.3.6.1.2.1.1.5.0"      # sysName: nombre (hostname) configurado en el equipo
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"    # ifDescr: descripción de cada interfaz
IF_ADMIN = "1.3.6.1.2.1.2.2.1.7"    # ifAdminStatus: estado CONFIGURADO (shutdown / no shutdown)
IF_OPER = "1.3.6.1.2.1.2.2.1.8"     # ifOperStatus: estado REAL del enlace (hay link o no)
IF_IN32 = "1.3.6.1.2.1.2.2.1.10"    # ifInOctets: bytes recibidos, contador de 32 bits
IF_OUT32 = "1.3.6.1.2.1.2.2.1.16"   # ifOutOctets: bytes enviados, contador de 32 bits
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"  # ifName: nombre corto (ej. "GE0/0/1", "port1")
# Contadores de 64 bits ("HC" = High Capacity). Un contador de 32 bits llega a su máximo
# (2^32 bytes ≈ 4,29 GB) y vuelve a 0 ("counter wrap"); en un enlace de 1 Gbps eso ocurre en
# ~34 segundos, lo que daría tasas de tráfico erróneas. Con 64 bits el desborde tardaría siglos.
IF_HCIN = "1.3.6.1.2.1.31.1.1.1.6"      # ifHCInOctets: bytes recibidos (64 bits)
IF_HCOUT = "1.3.6.1.2.1.31.1.1.1.10"    # ifHCOutOctets: bytes enviados (64 bits)
IF_HIGHSPEED = "1.3.6.1.2.1.31.1.1.1.15"  # ifHighSpeed: velocidad de la interfaz en Mbps
IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"    # ifAlias: descripción que el administrador le pone al puerto
# HOST-RESOURCES-MIB: estándar usado por Linux, MikroTik, etc. para CPU y memoria.
HR_CPU = "1.3.6.1.2.1.25.3.3.1.2"       # hrProcessorLoad: % de carga de cada procesador
HR_STO_TYPE = "1.3.6.1.2.1.25.2.3.1.2"  # hrStorageType: tipo de cada almacenamiento (RAM, disco...)
HR_STO_SIZE = "1.3.6.1.2.1.25.2.3.1.5"  # hrStorageSize: tamaño total (en unidades de asignación)
HR_STO_USED = "1.3.6.1.2.1.25.2.3.1.6"  # hrStorageUsed: cantidad usada (mismas unidades)
HR_TYPE_RAM = "1.3.6.1.2.1.25.2.1.2"    # valor de hrStorageType que significa "memoria RAM"

# ---- FORTINET-FORTIGATE-MIB ----
# 1.3.6.1.4.1 = rama "enterprises" (MIBs privadas); 12356 = número asignado a Fortinet.
FG_VERSION = "1.3.6.1.4.1.12356.101.4.1.1.0"    # versión de FortiOS
FG_CPU = "1.3.6.1.4.1.12356.101.4.1.3.0"        # % de uso de CPU
FG_MEM = "1.3.6.1.4.1.12356.101.4.1.4.0"        # % de uso de memoria
FG_DISK_USED = "1.3.6.1.4.1.12356.101.4.1.6.0"  # disco usado (MB)
FG_DISK_CAP = "1.3.6.1.4.1.12356.101.4.1.7.0"   # capacidad del disco (MB)
FG_SESSIONS = "1.3.6.1.4.1.12356.101.4.1.8.0"   # sesiones activas en el firewall

# ---- HUAWEI-ENTITY-EXTENT-MIB (hwEntityStateTable) ----
# 2011 = número de empresa de Huawei. Son tablas con una fila por "entidad" física (tarjetas,
# placas, ventiladores...). Muchas filas valen 0 porque no aplican; por eso se usa _max_valid.
HW_CPU = "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5"   # hwEntityCpuUsage (%)
HW_MEM = "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.7"   # hwEntityMemUsage (%)
HW_TEMP = "1.3.6.1.4.1.2011.5.25.31.1.1.1.1.11"  # hwEntityTemperature (°C)
HW_CPU_OLD = "1.3.6.1.4.1.2011.6.3.4.1.2"  # hwCpuDevDuty (AR / VRP antiguos)

# Traducción de los valores numéricos de ifAdminStatus / ifOperStatus (definidos en IF-MIB)
# a texto legible.
STATUS = {1: "up", 2: "down", 3: "testing", 4: "unknown", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}


def _max_valid(values, lo=0, hi=100):
    """Devuelve el mayor valor numérico dentro del rango (lo, hi], o None si no hay ninguno.

    Se usa con las tablas de Huawei: hay una fila por cada componente físico y muchas valen 0
    (componentes sin CPU, sin sensor...). El valor más alto representa al componente más
    cargado (normalmente la tarjeta principal), que es el que interesa vigilar.

    Parámetros:
        values: valores obtenidos de un walk.
        lo:     límite inferior (excluido). Con 0 se descartan los ceros "no aplica".
        hi:     límite superior (incluido). Descarta valores absurdos.

    Retorna:
        El máximo como float, o None.
    """
    vals = [float(v) for v in values if isinstance(v, (int, float)) and lo < v <= hi]
    return max(vals) if vals else None


class BaseDriver:
    """Driver genérico: lee lo estándar (MIB-II, IF-MIB y HOST-RESOURCES-MIB).

    Sirve directamente para equipos "generic" y es la clase padre de los drivers de cada marca,
    que solo reemplazan vendor_metrics() (herencia y polimorfismo).
    """
    vendor = "generic"

    def __init__(self, dev: dict, general: dict):
        """Crea el cliente SNMP del equipo y compila el filtro de interfaces a excluir.

        Parámetros:
            dev:     configuración del equipo (host, port, snmp, exclude_interfaces...).
            general: sección "general" de la configuración (snmp_timeout, snmp_retries).
        """
        self.dev = dev
        self.client = SnmpClient(
            dev["host"], dev["snmp"], port=dev.get("port", 161),
            timeout=general["snmp_timeout"], retries=general["snmp_retries"],
        )
        # Expresión regular (sin distinguir mayúsculas) de interfaces que se ignoran.
        # Si no hay filtro se usa r"^$", que solo coincide con un nombre vacío.
        self.exclude = re.compile(dev.get("exclude_interfaces") or r"^$", re.I)

    async def poll(self) -> dict:
        """Hace un sondeo completo del equipo y devuelve el "snapshot" normalizado.

        Retorna:
            Diccionario con datos del sistema, métricas e interfaces (formato del encabezado).

        Si el equipo no responde, SnmpClient lanza SnmpError y el monitor lo trata como fallo.
        """
        # Un solo GET con los 3 escalares del sistema (un solo paquete de ida y vuelta).
        sysv = await self.client.get(SYS_DESCR, SYS_UPTIME, SYS_NAME)
        snap = {
            "sys_descr": sysv.get(SYS_DESCR) or "",
            # sysUpTime viene en centésimas de segundo (TimeTicks); al dividir entre 100 queda
            # en segundos.
            "uptime": int((sysv.get(SYS_UPTIME) or 0) / 100),
            "sys_name": sysv.get(SYS_NAME) or "",
            # Métricas que dependen del fabricante; se llenan con vendor_metrics().
            "cpu": None, "mem": None, "temp": None, "sessions": None, "disk": None, "version": None,
        }
        snap["interfaces"] = await self.interfaces()
        # Se copian solo los valores que el driver sí obtuvo (los None no sobrescriben nada).
        snap.update({k: v for k, v in (await self.vendor_metrics()).items() if v is not None})
        return snap

    async def interfaces(self) -> dict:
        """Lee la tabla de interfaces (IF-MIB) y la combina en un diccionario por ifIndex.

        Cada columna (nombre, estados, contadores...) se obtiene con un WALK independiente; luego
        se unen por el índice de la fila (ifIndex), que es común a todas las columnas.

        Retorna:
            {ifIndex: {"name", "alias", "admin", "oper", "in_octets", "out_octets", "speed_mbps"}}
            sin las interfaces que coinciden con el filtro de exclusión.
        """
        c = self.client
        descr = await c.walk(IF_DESCR)
        names = await c.walk(IF_NAME)
        admin = await c.walk(IF_ADMIN)
        oper = await c.walk(IF_OPER)
        # Se prefieren los contadores de 64 bits (ver comentario de IF_HCIN arriba).
        hcin = await c.walk(IF_HCIN)
        hcout = await c.walk(IF_HCOUT)
        if not hcin:  # equipos sin contadores de 64 bits
            # Plan B: contadores de 32 bits (en SNMPv1 no existen los Counter64).
            hcin = await c.walk(IF_IN32)
            hcout = await c.walk(IF_OUT32)
        speed = await c.walk(IF_HIGHSPEED)
        alias = await c.walk(IF_ALIAS)
        out = {}
        # ifDescr existe en todos los equipos, así que se usa como lista "maestra" de interfaces.
        for idx, d in descr.items():
            # Se prefiere ifName (más corto); si no existe se usa ifDescr.
            name = names.get(idx) or d
            if self.exclude.search(str(name)):
                continue
            out[idx] = {
                "name": str(name),
                "alias": str(alias.get(idx) or ""),
                # admin = lo que configuró el administrador (shutdown -> "down").
                # oper  = el estado real del enlace (sin cable o extremo apagado -> "down").
                # Comparar ambos permite distinguir "la apagaron a propósito" de "se cayó".
                "admin": STATUS.get(admin.get(idx), "unknown"),
                "oper": STATUS.get(oper.get(idx), "unknown"),
                # Contadores acumulados de bytes. La tasa (bps) se calcula en monitor.py
                # comparando dos lecturas consecutivas.
                "in_octets": hcin.get(idx),
                "out_octets": hcout.get(idx),
                "speed_mbps": speed.get(idx),
            }
        return out

    async def vendor_metrics(self) -> dict:
        """Genérico (MikroTik, VyOS, Linux, etc.): HOST-RESOURCES-MIB.

        Retorna:
            {"cpu": promedio de carga de todos los procesadores (%) o None,
             "mem": % de RAM usada o None}
        """
        c = self.client
        # hrProcessorLoad trae una fila por núcleo/procesador; se promedian.
        cpu = await c.walk(HR_CPU)
        vals = [v for v in cpu.values() if isinstance(v, int)]
        mem = None
        # hrStorageTable mezcla RAM, discos, swap, etc. Se busca la fila cuyo tipo sea RAM.
        types = await c.walk(HR_STO_TYPE)
        ram = [i for i, t in types.items() if str(t) == HR_TYPE_RAM]
        if ram:
            size = await c.walk(HR_STO_SIZE)
            used = await c.walk(HR_STO_USED)
            i = ram[0]
            # Se evita dividir entre cero si el tamaño no viene o vale 0.
            if size.get(i):
                # Usado / total * 100 = porcentaje. Las unidades se cancelan (ambos en las
                # mismas unidades de asignación), así que no hace falta convertir a bytes.
                mem = round(100 * (used.get(i) or 0) / size[i], 1)
        return {"cpu": sum(vals) / len(vals) if vals else None, "mem": mem}


class FortiGateDriver(BaseDriver):
    """Driver para firewalls FortiGate: usa la MIB privada de Fortinet."""
    vendor = "fortigate"

    async def vendor_metrics(self) -> dict:
        """Obtiene versión, CPU, memoria, sesiones y % de disco del FortiGate con un solo GET.

        Retorna:
            {"version", "cpu", "mem", "sessions", "disk"}; los que no existan quedan en None.
        """
        # FortiGate entrega estos datos como escalares (.0), así que basta un GET (sin walks).
        v = await self.client.get(FG_VERSION, FG_CPU, FG_MEM, FG_DISK_USED, FG_DISK_CAP, FG_SESSIONS)
        disk = None
        # El porcentaje de disco se calcula (usado / capacidad); se evita dividir entre cero.
        if v.get(FG_DISK_CAP):
            disk = round(100 * (v.get(FG_DISK_USED) or 0) / v[FG_DISK_CAP], 1)
        return {
            "version": v.get(FG_VERSION),
            "cpu": v.get(FG_CPU),
            "mem": v.get(FG_MEM),
            "sessions": v.get(FG_SESSIONS),
            "disk": disk,
        }


class HuaweiDriver(BaseDriver):
    """Driver para routers y switches Huawei (VRP): usa HUAWEI-ENTITY-EXTENT-MIB."""
    vendor = "huawei"

    async def vendor_metrics(self) -> dict:
        """Obtiene CPU, memoria y temperatura del equipo Huawei, con varios planes de respaldo.

        Retorna:
            {"cpu", "mem", "temp"} (valores o None).
        """
        c = self.client
        cpu = _max_valid((await c.walk(HW_CPU)).values())
        mem = _max_valid((await c.walk(HW_MEM)).values())
        # Para temperatura se acepta un rango hasta 150 °C (no es un porcentaje).
        temp = _max_valid((await c.walk(HW_TEMP)).values(), 0, 150)
        # Plan B: equipos antiguos (routers AR, VRP viejos) publican la CPU en otro OID.
        if cpu is None:
            cpu = _max_valid((await c.walk(HW_CPU_OLD)).values())
        # Plan C: probar la MIB estándar HOST-RESOURCES (método de la clase padre).
        if cpu is None:
            cpu = (await super().vendor_metrics()).get("cpu")
        return {"cpu": cpu, "mem": mem, "temp": temp}


# Tabla que asocia el valor "vendor" de config.yaml con la clase de driver correspondiente.
DRIVERS = {"fortigate": FortiGateDriver, "huawei": HuaweiDriver, "generic": BaseDriver}


def make_driver(dev: dict, general: dict) -> BaseDriver:
    """Fábrica de drivers: crea el driver adecuado según el fabricante del equipo.

    Parámetros:
        dev:     configuración del equipo (dev["vendor"] ya validado en config.py).
        general: sección "general" de la configuración.

    Retorna:
        Una instancia de FortiGateDriver, HuaweiDriver o BaseDriver.
    """
    return DRIVERS[dev["vendor"]](dev, general)
