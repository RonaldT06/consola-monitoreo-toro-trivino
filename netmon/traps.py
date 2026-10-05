"""Receptor de traps SNMP v1/v2c (UDP 162 por defecto).

Rol de este módulo en el sistema:
  El sondeo normal (monitor.py) PREGUNTA a los equipos cada cierto tiempo. Los TRAPS funcionan
  al revés: es el equipo el que AVISA por iniciativa propia, enviando un paquete UDP al puerto
  162 del servidor de monitoreo en cuanto ocurre algo (una interfaz cae, el equipo arranca, un
  túnel VPN se cae...). Esto permite reaccionar en menos de un segundo.

  - Recibe: la sección "traps" de la configuración (dirección, puerto y communities aceptadas).
  - Produce: por cada trap recibido llama a Monitor.on_trap(ip_origen, oid_del_trap, varbinds),
    que decide si generar un evento o solo forzar un sondeo inmediato.

Para que funcione, cada equipo debe configurarse para enviar traps a la IP de este servidor
con la misma community. El puerto 162 normalmente requiere permisos de administrador.
"""
from __future__ import annotations

import asyncio

from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import ntfrcv

# snmpTrapOID.0: en un trap v2c, el varbind con este OID indica QUÉ trap es (su "tipo").
SNMP_TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"


def start_trap_receiver(cfg: dict, monitor) -> engine.SnmpEngine:
    """Abre el puerto UDP de traps y registra la función que procesa cada trap recibido.

    Parámetros:
        cfg:     sección "traps" de la configuración (listen, port, communities).
        monitor: objeto Monitor al que se le entregan los traps.

    Retorna:
        El SnmpEngine receptor (se devuelve para que siga existiendo mientras corre el programa).
    """
    # Motor SNMP propio para la recepción (independiente del que se usa para consultar).
    eng = engine.SnmpEngine()
    # Abre un socket UDP en modo servidor (escuchando) en la IP y puerto configurados.
    config.add_transport(
        eng, udp.DOMAIN_NAME,
        udp.UdpTransport().open_server_mode((cfg.get("listen", "0.0.0.0"), int(cfg.get("port", 162)))),
    )
    # Registra cada community aceptada (v1/v2c). Los traps con una community distinta se
    # descartan. "area-{i}" es solo un nombre interno para cada entrada.
    for i, comm in enumerate(cfg.get("communities") or ["public"]):
        config.add_v1_system(eng, f"area-{i}", comm)

    # Se guarda el bucle de asyncio actual para poder crear tareas desde el callback.
    loop = asyncio.get_event_loop()

    def callback(snmp_engine, state_ref, ctx_engine_id, ctx_name, var_binds, cb_ctx):
        """Función que pysnmp llama (de forma SÍNCRONA) cada vez que llega un trap válido.

        Parámetros (los define pysnmp; aquí solo se usan snmp_engine y var_binds):
            snmp_engine: motor que recibió el trap (permite consultar la IP de origen).
            var_binds:   lista de pares (OID, valor) que trae el trap.

        No retorna nada: programa el procesamiento asíncrono en Monitor.on_trap.
        """
        # La IP de origen no viene en los varbinds: se consulta el "contexto de ejecución"
        # del mensaje recibido, que guarda la dirección de transporte (IP, puerto).
        ctx = snmp_engine.observer.get_execution_context("rfc3412.receiveMessage:request")
        src_ip = ctx["transportAddress"][0]
        # Se convierten OIDs y valores a texto para trabajar fácilmente con ellos.
        binds = [(str(o), v.prettyPrint()) for o, v in var_binds]
        # Busca el varbind snmpTrapOID.0 para saber qué tipo de trap es ("" si no viene).
        trap_oid = next((v for o, v in binds if o == SNMP_TRAP_OID), "")
        # El callback es una función normal (no async), así que no puede usar "await".
        # create_task programa on_trap() para que se ejecute en el bucle de asyncio.
        loop.create_task(monitor.on_trap(src_ip, trap_oid, binds))

    # Registra el receptor de notificaciones (traps/informs) con el callback anterior.
    ntfrcv.NotificationReceiver(eng, callback)
    print(f"[traps] escuchando en UDP {cfg.get('listen')}:{cfg.get('port')}")
    return eng
