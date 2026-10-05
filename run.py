#!/usr/bin/env python3
"""NetMon — Dashboard propio de monitoreo (FortiGate + Huawei) con alertas a Telegram.

Este archivo es el PUNTO DE ENTRADA del programa: es el que se ejecuta desde la consola.
Su trabajo es "armar" el sistema completo y ponerlo a funcionar:

  1. Lee los argumentos de la línea de comandos (--config, --simulate, --no-telegram, --port).
  2. Carga variables de entorno desde el archivo .env y la configuración desde config.yaml
     (módulo netmon/config.py).
  3. Crea el objeto Monitor (netmon/monitor.py), que es el núcleo: consulta los equipos por
     SNMP de forma periódica y genera las alertas.
  4. Si está habilitado, crea el bot de Telegram (netmon/telegram.py) y lo "suscribe" a los
     eventos del monitor para que cada alerta llegue al chat.
  5. Si está habilitado, abre el receptor de traps SNMP (netmon/traps.py) en UDP.
  6. Crea la aplicación web (netmon/web.py) y la sirve con uvicorn (servidor HTTP asíncrono).
  7. Ejecuta todas estas tareas AL MISMO TIEMPO dentro de un solo bucle de asyncio.

Uso:
  python run.py                      # usa config.yaml
  python run.py --config lab.yaml    # otro archivo
  python run.py --simulate           # equipos virtuales (sin PNETLab) para probar
"""
from __future__ import annotations

import argparse   # lectura de argumentos de la línea de comandos
import asyncio    # programación asíncrona: muchas tareas concurrentes en un solo hilo
import os
import socket     # se usa solo para comprobar si un puerto está libre
import sys

import uvicorn    # servidor ASGI que atiende las peticiones HTTP/WebSocket de FastAPI

from pathlib import Path

from netmon.config import ConfigError, load_config, load_dotenv
from netmon.monitor import Monitor
from netmon.web import create_app


def port_error(host: str, port: int, udp: bool) -> str | None:
    """Devuelve None si el puerto se puede usar, o el error del sistema operativo.

    ¿Por qué se revisa el puerto ANTES de arrancar? Si el puerto ya está ocupado (por otra
    instancia de NetMon, por otro programa, o porque los puertos < 1024 como el 162 de traps
    exigen permisos de administrador), uvicorn o pysnmp fallarían más adelante con un error
    poco claro y en medio de otras tareas. Probando primero se puede mostrar un mensaje
    entendible y sugerir una solución.

    Parámetros:
        host: dirección IP local donde se quiere escuchar (ej. "0.0.0.0" = todas las interfaces).
        port: número de puerto a probar.
        udp:  True para probar un socket UDP (traps SNMP); False para TCP (dashboard web).

    Retorna:
        None si el puerto está libre, o el texto del error (str) si no se pudo usar.
    """
    # SOCK_DGRAM = UDP (sin conexión, lo usa SNMP); SOCK_STREAM = TCP (lo usa HTTP).
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    try:
        # bind() "reserva" el puerto. Si otro proceso lo tiene, el sistema lanza OSError.
        s.bind((host, port))
        return None
    except OSError as e:
        return str(e)
    finally:
        # Se cierra el socket de prueba para liberar el puerto y que lo use el servicio real.
        s.close()


async def main(args):
    """Arma todos los componentes de NetMon y los ejecuta de forma concurrente.

    Parámetros:
        args: objeto con los argumentos de la línea de comandos (resultado de argparse).

    No retorna nada: se queda ejecutando hasta que el programa se detenga (Ctrl+C).
    """
    # Carga config.yaml (con valores por defecto y validaciones). Puede lanzar ConfigError.
    cfg = load_config(args.config)
    # El argumento --port tiene prioridad sobre el puerto escrito en config.yaml.
    if args.port:
        cfg["web"]["port"] = args.port
    web = cfg["web"]
    # Verificación previa del puerto TCP del dashboard (ver docstring de port_error).
    err = port_error(web["host"], int(web["port"]), udp=False)
    if err:
        sys.exit(f"No se puede usar el puerto web {web['port']}: {err}\n"
                 f"Usa otro puerto:  python run.py --port 8090   (o cambia web.port en config.yaml)")
    if args.no_telegram:
        cfg["telegram"]["enabled"] = False
    # El Monitor es el núcleo: crea un "DeviceState" por cada equipo de la configuración.
    mon = Monitor(cfg, simulate=args.simulate)

    # Lista de corrutinas que se ejecutarán en paralelo. mon.run() es el bucle de sondeo SNMP.
    tasks = [mon.run()]
    telegram = None
    if cfg["telegram"]["enabled"]:
        # Importación "perezosa": solo se carga el módulo de Telegram (y httpx) si se va a usar.
        from netmon.telegram import Telegram
        telegram = Telegram(cfg["telegram"], mon)
        # Patrón "observador": el monitor llamará a telegram.on_event() cada vez que emita un
        # evento (alerta), sin que el monitor tenga que saber nada de Telegram.
        mon.event_listeners.append(telegram.on_event)
        tasks.append(telegram.run())
    else:
        print("[telegram] deshabilitado")

    # En modo simulador no se abren traps: los equipos son virtuales y no envían nada por la red.
    if cfg["traps"]["enabled"] and not args.simulate:
        from netmon.traps import start_trap_receiver
        tr = cfg["traps"]
        # El puerto 162/UDP es el estándar de traps; en Linux/Windows suele requerir permisos
        # de administrador. Por eso se prueba antes y, si falla, se sigue funcionando sin traps.
        terr = port_error(tr["listen"], int(tr["port"]), udp=True)
        if not terr:
            start_trap_receiver(tr, mon)
        else:
            print(f"[traps] no se pudo abrir UDP {tr['port']} ({terr}). Ejecuta como administrador "
                  f"o usa otro puerto (ej. 1162). Se continúa sin traps.")

    # Aplicación web (API REST + WebSocket + archivos del dashboard).
    app = create_app(mon, telegram)
    # Se crea el servidor uvicorn "a mano" (en vez de uvicorn.run) para poder ejecutarlo
    # dentro del MISMO bucle de asyncio que el monitor y Telegram.
    server = uvicorn.Server(uvicorn.Config(app, host=web["host"], port=int(web["port"]), log_level="warning"))
    print(f"[web] dashboard en http://localhost:{web['port']}  (red: http://<IP-de-este-PC>:{web['port']})")
    tasks.append(server.serve())
    # asyncio.gather ejecuta todas las corrutinas a la vez (concurrencia cooperativa) y espera
    # a que terminen. En la práctica nunca terminan: son bucles infinitos de servicio.
    await asyncio.gather(*tasks)


# Este bloque solo se ejecuta cuando el archivo se corre directamente (python run.py),
# no cuando se importa desde otro módulo.
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="NetMon")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--simulate", action="store_true", help="usa equipos simulados")
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--port", type=int, help="puerto del dashboard (por defecto el de config.yaml)")
    a = p.parse_args()
    # Carga las variables del archivo .env (tokens, contraseñas SNMP...) en os.environ.
    load_dotenv(Path.cwd() / ".env")
    # También se puede activar el simulador con la variable de entorno NETMON_SIMULATE
    # (útil, por ejemplo, al ejecutar en Docker sin cambiar el comando).
    if os.environ.get("NETMON_SIMULATE", "").lower() in ("1", "true", "si", "yes"):
        a.simulate = True
    try:
        # asyncio.run crea el bucle de eventos, ejecuta main() y lo cierra al terminar.
        asyncio.run(main(a))
    except ConfigError as e:
        # Errores de configuración se muestran como un mensaje limpio, sin traza de Python.
        sys.exit(f"Error de configuración: {e}")
    except KeyboardInterrupt:
        # Ctrl+C: salida ordenada.
        print("\nDetenido.")
