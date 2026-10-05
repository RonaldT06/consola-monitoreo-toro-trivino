"""Cliente SNMP asíncrono (pysnmp 7) con soporte v2c y v3.

Este módulo es la CAPA DE COMUNICACIÓN con los equipos de red. Encapsula la librería pysnmp
para que el resto del programa pueda pedir datos con dos operaciones sencillas:

  - get(oid1, oid2, ...): pide valores puntuales (escalares), ej. el nombre o el uptime.
  - walk(oid):            recorre una COLUMNA de una tabla SNMP, ej. el estado de todas las
                          interfaces, y devuelve un diccionario {índice: valor}.

Conceptos básicos de SNMP usados aquí:
  - OID (Object Identifier): "dirección" numérica de un dato dentro de la MIB, por ejemplo
    1.3.6.1.2.1.1.5.0 = sysName (nombre del equipo). Es como una ruta en un árbol jerárquico.
  - MIB: base de información que define qué significa cada OID (MIB-II, IF-MIB, MIBs privadas).
  - Agente SNMP: el servicio dentro del router/switch/firewall que responde en UDP 161.
  - SNMPv1/v2c: se autentican con una "community" (una palabra clave en texto plano).
  - SNMPv3: usa el modelo USM (User-based Security Model): usuario + autenticación (MD5/SHA)
    + cifrado opcional (DES/AES). Es mucho más seguro.

Qué recibe: IP/puerto del equipo y el bloque "snmp" de su configuración.
Qué produce: diccionarios de Python con valores ya convertidos (int, str o None).
Quién lo usa: drivers.py (cada driver crea un SnmpClient). sim.py solo reutiliza SnmpError.
"""
from __future__ import annotations

from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    bulk_walk_cmd,
    get_cmd,
    usm3DESEDEPrivProtocol,
    usmAesCfb128Protocol,
    usmAesCfb256Protocol,
    usmDESPrivProtocol,
    usmHMAC192SHA256AuthProtocol,
    usmHMACMD5AuthProtocol,
    usmHMACSHAAuthProtocol,
    usmNoAuthProtocol,
    usmNoPrivProtocol,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject

# Traducción de los nombres que escribe el usuario en config.yaml (auth_protocol) a los
# objetos de pysnmp. Son los algoritmos de AUTENTICACIÓN de SNMPv3 (garantizan que el mensaje
# viene de quien dice y no fue alterado).
AUTH = {
    "none": usmNoAuthProtocol,
    "md5": usmHMACMD5AuthProtocol,
    "sha": usmHMACSHAAuthProtocol,
    "sha256": usmHMAC192SHA256AuthProtocol,
}
# Algoritmos de PRIVACIDAD (cifrado) de SNMPv3 (priv_protocol): evitan que alguien que capture
# el tráfico pueda leer los datos.
PRIV = {
    "none": usmNoPrivProtocol,
    "des": usmDESPrivProtocol,
    "3des": usm3DESEDEPrivProtocol,
    "aes": usmAesCfb128Protocol,
    "aes128": usmAesCfb128Protocol,
    "aes256": usmAesCfb256Protocol,
}

# Motor SNMP compartido por todo el programa (se crea una sola vez, al primer uso).
_ENGINE: SnmpEngine | None = None


def engine() -> SnmpEngine:
    """Devuelve el motor SNMP único (patrón "singleton"), creándolo la primera vez.

    El SnmpEngine guarda estado interno (caché de usuarios v3, contadores, despachador de
    mensajes). Compartir uno solo para todos los equipos es más eficiente que crear uno por
    petición.

    Retorna:
        La instancia de SnmpEngine compartida.
    """
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = SnmpEngine()
    return _ENGINE


class SnmpError(Exception):
    """Error de comunicación SNMP (timeout, credenciales incorrectas, error del agente...).

    El monitor la interpreta como "el sondeo falló" y decide si el equipo está caído o si
    solo falla SNMP.
    """
    pass


def _auth_data(snmp: dict):
    """Construye el objeto de credenciales que pysnmp necesita según la versión SNMP.

    Parámetros:
        snmp: bloque "snmp" de la configuración del equipo (version, community, user,
              auth_key, auth_protocol, priv_key, priv_protocol).

    Retorna:
        CommunityData (para v1/v2c) o UsmUserData (para v3).
    """
    ver = snmp.get("version", "2c")
    if ver in ("1", "2c"):
        # mpModel = "message processing model": 0 = SNMPv1, 1 = SNMPv2c.
        return CommunityData(snmp.get("community", "public"), mpModel=0 if ver == "1" else 1)
    # ---- SNMPv3 (USM) ----
    auth_p = str(snmp.get("auth_protocol", "sha")).lower()
    priv_p = str(snmp.get("priv_protocol", "aes")).lower()
    kwargs = {}
    # Niveles de seguridad de SNMPv3 según las claves que se configuren:
    #   sin auth_key ni priv_key -> noAuthNoPriv (solo usuario)
    #   con auth_key             -> authNoPriv   (autenticado, sin cifrar)
    #   con auth_key y priv_key  -> authPriv     (autenticado y cifrado)
    if snmp.get("auth_key"):
        kwargs["authKey"] = snmp["auth_key"]
        # Si el nombre del protocolo no se reconoce se usa SHA como valor seguro por defecto.
        kwargs["authProtocol"] = AUTH.get(auth_p, usmHMACSHAAuthProtocol)
    if snmp.get("priv_key"):
        kwargs["privKey"] = snmp["priv_key"]
        kwargs["privProtocol"] = PRIV.get(priv_p, usmAesCfb128Protocol)
    return UsmUserData(snmp["user"], **kwargs)


def _convert(val):
    """Convierte un valor SNMP de pysnmp (tipos ASN.1) a un tipo simple de Python.

    Parámetros:
        val: valor recibido en una respuesta SNMP.

    Retorna:
        - None si el agente dijo que el dato no existe (noSuchObject/noSuchInstance) o si se
          llegó al final de la MIB (endOfMibView).
        - int para tipos numéricos (contadores, gauges, TimeTicks...).
        - str para textos (OctetString) y OIDs.
    """
    # En SNMPv2c pedir un OID que el equipo no implementa NO es un error de protocolo: el agente
    # responde con uno de estos valores especiales. Aquí se tratan como "dato no disponible".
    if isinstance(val, (NoSuchObject, NoSuchInstance, EndOfMibView)):
        return None
    cls = val.__class__.__name__
    # Tipos numéricos de SNMP:
    #   Counter32/Counter64: contadores que solo crecen (ej. bytes recibidos); Counter64 = 64 bits.
    #   Gauge32: valor que sube y baja (ej. velocidad de interfaz).
    #   TimeTicks: tiempo en CENTÉSIMAS de segundo (ej. sysUpTime).
    if cls in ("Integer", "Integer32", "Counter32", "Counter64", "Gauge32", "Unsigned32", "TimeTicks"):
        return int(val)
    if cls == "OctetString":
        try:
            # OctetString son bytes; normalmente texto. Se decodifica como UTF-8 (reemplazando
            # bytes inválidos) y se quitan los caracteres nulos que algunos equipos añaden al final.
            return bytes(val).decode("utf-8", "replace").strip("\x00")
        except Exception:
            return val.prettyPrint()
    if cls == "ObjectIdentifier":
        # Hay datos cuyo VALOR es un OID (ej. hrStorageType); se devuelve como texto "1.3.6...".
        return str(val)
    # Cualquier otro tipo (IpAddress, etc.) se devuelve en su forma legible.
    return val.prettyPrint()


class SnmpClient:
    """Cliente SNMP para UN equipo: guarda su dirección, credenciales y parámetros de espera.

    Se usa con "await" porque todas las operaciones son asíncronas: mientras se espera la
    respuesta UDP de un equipo, el programa puede seguir atendiendo a los demás.
    """

    def __init__(self, host: str, snmp: dict, port: int = 161, timeout: float = 2, retries: int = 1):
        """Prepara el cliente (todavía no envía nada por la red).

        Parámetros:
            host:    IP o nombre del equipo.
            snmp:    bloque "snmp" de la configuración (versión y credenciales).
            port:    puerto UDP del agente SNMP (161 por defecto).
            timeout: segundos de espera por cada respuesta.
            retries: cuántas veces se reintenta antes de dar la petición por fallida.
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.auth = _auth_data(snmp)
        # El destino UDP se crea más tarde (_tgt) porque su creación es asíncrona.
        self._target = None

    async def _tgt(self):
        """Devuelve el destino UDP del equipo, creándolo solo la primera vez (se reutiliza).

        Retorna:
            Objeto UdpTransportTarget con IP, puerto, timeout y reintentos.
        """
        if self._target is None:
            # create() es asíncrono porque puede resolver nombres DNS.
            self._target = await UdpTransportTarget.create(
                (self.host, self.port), timeout=self.timeout, retries=self.retries
            )
        return self._target

    async def get(self, *oids: str) -> dict[str, object]:
        """GET de varios OIDs. Devuelve {oid: valor|None}.

        Una operación GET pide valores EXACTOS (OIDs que terminan en .0 para escalares, como
        sysName.0). Se pueden pedir varios OIDs en un solo paquete, lo que ahorra viajes de red.

        Parámetros:
            *oids: uno o más OIDs en texto, ej. "1.3.6.1.2.1.1.5.0".

        Retorna:
            Diccionario {oid_en_texto: valor_convertido_o_None}.

        Lanza SnmpError si no hay respuesta o el agente devuelve un error.
        """
        tgt = await self._tgt()
        # get_cmd devuelve 4 elementos:
        #   err_ind  -> error de transporte/seguridad (ej. timeout, usuario v3 incorrecto)
        #   err_stat -> error reportado por el agente en la respuesta (errorStatus del PDU)
        #   err_idx  -> posición del OID que causó el error (no se usa aquí)
        #   binds    -> lista de pares (OID, valor), llamados "varbinds"
        err_ind, err_stat, err_idx, binds = await get_cmd(
            engine(), self.auth, tgt, ContextData(), *[ObjectType(ObjectIdentity(o)) for o in oids]
        )
        if err_ind:
            raise SnmpError(str(err_ind))
        if err_stat:
            # En v2c un OID inexistente devuelve noSuchObject, no error. Otro error = fallo real
            raise SnmpError(err_stat.prettyPrint())
        return {str(n): _convert(v) for n, v in binds}

    async def walk(self, oid: str) -> dict[str, object]:
        """Walk de una columna. Devuelve {sufijo_índice: valor}.

        Un WALK recorre todos los OIDs que están "debajo" de un OID base. Se usa para las tablas
        SNMP: por ejemplo, ifOperStatus (1.3.6.1.2.1.2.2.1.8) tiene una fila por interfaz:
        ...2.2.1.8.1, ...2.2.1.8.2, etc. El número final es el ÍNDICE (ifIndex) de la fila.

        Se usa BULK walk (GETBULK, existe desde SNMPv2c): en vez de pedir un valor por paquete
        (GETNEXT), se piden hasta 25 valores por paquete, lo que es mucho más rápido en tablas
        grandes.

        Parámetros:
            oid: OID base de la columna a recorrer (sin el índice).

        Retorna:
            Diccionario {índice_en_texto: valor}, ej. {"1": 1, "2": 2} para ifOperStatus.
        """
        tgt = await self._tgt()
        out: dict[str, object] = {}
        # Prefijo que deben tener todos los OIDs de la columna; sirve para detectar cuándo el
        # recorrido ya "se salió" de la tabla pedida.
        prefix = oid.rstrip(".") + "."
        # Parámetros de GETBULK: nonRepeaters=0, maxRepetitions=25 (valores por paquete).
        # lexicographicMode=False indica a pysnmp que se detenga al salir del subárbol pedido.
        async for err_ind, err_stat, err_idx, binds in bulk_walk_cmd(
            engine(), self.auth, tgt, ContextData(), 0, 25,
            ObjectType(ObjectIdentity(oid)), lexicographicMode=False,
        ):
            if err_ind:
                raise SnmpError(str(err_ind))
            if err_stat:
                # Error del agente a mitad del recorrido: se devuelve lo obtenido hasta ahora.
                break
            for name, val in binds:
                n = str(name)
                # Un paquete GETBULK puede traer OIDs de la SIGUIENTE columna; al detectarlos
                # se termina el recorrido (protección adicional).
                if not n.startswith(prefix):
                    return out
                v = _convert(val)
                if v is not None:
                    # Se guarda solo el sufijo (el índice de la fila), ej. "3" para la interfaz 3.
                    out[n[len(prefix):]] = v
        return out
