"""Carga y validación de config.yaml.

Este módulo es la PUERTA DE ENTRADA de la configuración. Antes de monitorear cualquier equipo,
el programa necesita saber: qué equipos hay (IP, fabricante, credenciales SNMP), cada cuánto
consultarlos, qué umbrales de CPU/memoria/temperatura usar, y si Telegram y los traps están activos.

Qué recibe:
  - La ruta de un archivo YAML (normalmente config.yaml).
  - Opcionalmente un archivo .env con datos sensibles (token del bot, comunidades, claves SNMPv3).

Qué produce:
  - Un diccionario de Python ("cfg") ya completo y validado, con secciones:
    general, web, thresholds, telegram, traps y devices.

Cómo se conecta con el resto:
  - run.py llama a load_dotenv() y load_config() al arrancar.
  - monitor.py, drivers.py, telegram.py, traps.py y web.py solo leen el diccionario "cfg"
    resultante; nunca vuelven a leer el archivo YAML.

Detalles importantes:
  - Los secretos NO se escriben directamente en config.yaml: se escribe ${NOMBRE_VARIABLE}
    y el valor real se toma del archivo .env o de las variables de entorno del sistema.
  - Todo lo que el usuario no especifique se completa con los valores de DEFAULTS.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

import yaml

# Valores por defecto. Lo que el usuario escriba en config.yaml se "mezcla" encima de esto
# (ver _merge), así el archivo del usuario puede ser corto y solo indicar lo que cambia.
DEFAULTS = {
    "general": {
        "poll_interval": 10,      # segundos entre cada sondeo SNMP de un equipo
        "snmp_timeout": 2,        # segundos que se espera la respuesta de cada petición SNMP
        "snmp_retries": 1,        # reintentos SNMP antes de considerar que la petición falló
        "down_after": 2,          # sondeos fallidos seguidos necesarios para declarar un problema
        "history_points": 360,    # puntos de historial guardados en memoria (360 x 10 s = 1 hora)
        "db_path": "netmon.db",   # archivo SQLite donde se guarda el historial de eventos
    },
    "web": {"host": "0.0.0.0", "port": 8080},   # 0.0.0.0 = escuchar en todas las interfaces de red
    # Umbrales en %, °C. "sustain" = cuántos sondeos seguidos por encima del umbral se necesitan
    # para alertar (evita alertas por un pico momentáneo).
    "thresholds": {"cpu_high": 80, "mem_high": 85, "temp_high": 70, "disk_high": 90, "sustain": 2},
    "telegram": {"enabled": False, "bot_token": "", "chat_ids": [], "min_severity": "info", "commands": True},
    # 162/UDP es el puerto estándar donde los equipos envían traps SNMP.
    "traps": {"enabled": False, "listen": "0.0.0.0", "port": 162, "communities": ["public"]},
    "devices": [],
}

# Expresión regular de interfaces que NO interesa monitorear (interfaces internas o virtuales
# de FortiGate/Huawei: NULL0, InLoopBack0, Console, túneles ssl., l2t., naf., modem, npu0...).
# Se compara contra el nombre de la interfaz; las que coinciden se ignoran.
DEFAULT_EXCLUDE = r"^(NULL|InLoopBack|Console|ssl\.|l2t\.|naf\.|modem|npu\d|Sip-Tunnel)"
# Fabricantes soportados; cada uno tiene su "driver" en drivers.py.
VENDORS = {"fortigate", "huawei", "generic"}


class ConfigError(Exception):
    """Error en la configuración (archivo inexistente, campo faltante, valor inválido...).

    run.py la captura para mostrar un mensaje claro al usuario en lugar de una traza de Python.
    """
    pass


def _merge(base: dict, over: dict) -> dict:
    """Mezcla dos diccionarios de forma recursiva ("deep merge").

    Parámetros:
        base: diccionario con los valores por defecto.
        over: diccionario con los valores del usuario, que tienen prioridad.

    Retorna:
        Un diccionario NUEVO (no modifica "base") donde las sub-secciones se combinan clave
        por clave. Ej.: si el usuario solo pone general.poll_interval, se conservan los demás
        valores por defecto de "general".
    """
    # deepcopy evita modificar DEFAULTS por accidente (los diccionarios son mutables).
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            # Ambos son diccionarios: se mezclan recursivamente en vez de reemplazarse.
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_dotenv(*paths: Path) -> None:
    """Carga variables desde uno o varios archivos .env hacia os.environ.

    Un archivo .env tiene líneas del tipo NOMBRE=valor (las líneas vacías y las que empiezan
    con # se ignoran). Sirve para guardar secretos (token de Telegram, comunidad SNMP, claves
    SNMPv3) fuera de config.yaml, de modo que config.yaml se pueda compartir sin exponerlos.

    Parámetros:
        *paths: rutas de archivos .env a leer; los que no existan se saltan sin error.

    No retorna nada. Usa os.environ.setdefault, así que una variable que YA existe en el sistema
    NO se sobrescribe: las variables reales del sistema tienen prioridad sobre el archivo .env.
    """
    for env in paths:
        if not env.is_file():
            continue
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            # partition("=") separa solo en el PRIMER "=", así el valor puede contener "=".
            key, _, val = line.partition("=")
            # Se quitan espacios y comillas opcionales alrededor del valor ("valor" o 'valor').
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


# Expresión regular para encontrar ${VAR} o ${VAR:-valor_por_defecto} dentro del texto YAML.
#   grupo 1: nombre de la variable (letras, números y _; no puede empezar con número).
#   grupo 2: (opcional) valor por defecto que va después de ":-", igual que en bash.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(text: str) -> str:
    """Reemplaza ${VAR} y ${VAR:-defecto} en un texto por el valor de las variables de entorno.

    Parámetros:
        text: contenido completo del archivo YAML, como texto.

    Retorna:
        El mismo texto con las variables ya sustituidas.

    Lanza ConfigError si una variable no existe y no tiene valor por defecto, para que el
    usuario sepa exactamente qué falta en su .env.
    """
    def repl(m: re.Match) -> str:
        """Calcula el reemplazo para UNA coincidencia ${...} encontrada por la expresión regular.

        Parámetros:
            m: objeto Match con el nombre (grupo 1) y el valor por defecto (grupo 2, o None).

        Retorna:
            El valor de la variable de entorno, o el valor por defecto si no existe.
        """
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"Falta la variable {name}. Defínela en el archivo .env (mira .env.example)")
    # re.sub llama a repl() por cada ${...} encontrado y pega el resultado en el texto.
    # Las líneas de comentario (#) se dejan tal cual: pueden mencionar ${VARIABLES} como ejemplo.
    return "".join(
        line if line.lstrip().startswith("#") else _VAR.sub(repl, line)
        for line in text.splitlines(keepends=True)
    )


def load_config(path: str | Path) -> dict:
    """Lee, completa y valida el archivo de configuración YAML.

    Pasos: carga .env -> sustituye ${VAR} -> interpreta el YAML -> mezcla con DEFAULTS ->
    valida cada equipo -> normaliza la sección de Telegram.

    Parámetros:
        path: ruta del archivo YAML (ej. "config.yaml").

    Retorna:
        El diccionario de configuración final que usa el resto del programa.

    Lanza ConfigError ante cualquier problema, con un mensaje en español que explica qué corregir.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"No existe {p}. Copia config.example.yaml como config.yaml")
    # Se busca .env tanto en la carpeta actual como en la carpeta donde está el YAML.
    load_dotenv(Path.cwd() / ".env", p.resolve().parent / ".env")
    # Primero se expanden las variables en el TEXTO y luego se interpreta el YAML.
    # safe_load solo crea tipos básicos (dict, list, str, int...), nunca objetos arbitrarios.
    # "or {}" cubre el caso de un archivo vacío (safe_load devuelve None).
    raw = yaml.safe_load(expand_env(p.read_text(encoding="utf-8"))) or {}
    cfg = _merge(DEFAULTS, raw)
    # La variable NETMON_DB permite cambiar la ruta de la base de datos (útil en Docker).
    if os.environ.get("NETMON_DB"):
        cfg["general"]["db_path"] = os.environ["NETMON_DB"]

    # ---- Validación de cada equipo ----
    names = set()   # para detectar nombres repetidos
    for d in cfg["devices"]:
        # Trampa clásica de YAML: palabras como ON, OFF, YES, NO se leen como True/False.
        # Si un equipo se llama así sin comillas, su nombre llega como booleano.
        if isinstance(d.get("name"), bool):
            raise ConfigError("Un nombre de equipo se leyó como sí/no (ej. OFF/ON/YES). Ponlo entre comillas")
        # Campos obligatorios mínimos.
        for key in ("name", "host", "vendor"):
            if not d.get(key):
                raise ConfigError(f"Equipo sin '{key}': {d}")
        # El nombre se usa como identificador único en todo el sistema (claves de alertas, API web).
        if d["name"] in names:
            raise ConfigError(f"Nombre de equipo repetido: {d['name']}")
        names.add(d["name"])
        d["vendor"] = str(d["vendor"]).lower()
        if d["vendor"] not in VENDORS:
            raise ConfigError(f"{d['name']}: vendor debe ser uno de {sorted(VENDORS)}")
        # Valores por defecto por equipo (setdefault solo asigna si la clave no existe).
        d.setdefault("role", d["vendor"].capitalize())
        d.setdefault("port", 161)              # 161/UDP = puerto estándar del agente SNMP
        d.setdefault("ping", True)             # usar ping para distinguir "caído" de "falla SNMP"
        d.setdefault("exclude_interfaces", DEFAULT_EXCLUDE)
        # Cada equipo puede tener umbrales propios; los que no defina se heredan de los globales.
        d["thresholds"] = _merge(cfg["thresholds"], d.get("thresholds") or {})
        snmp = d.setdefault("snmp", {})
        # Se convierte a texto porque en YAML "3" sin comillas se lee como número entero.
        snmp["version"] = str(snmp.get("version", "2c"))
        if snmp["version"] not in ("1", "2c", "3"):
            raise ConfigError(f"{d['name']}: snmp.version debe ser 2c o 3")
        # SNMPv3 no usa "community": se autentica con un usuario (modelo USM).
        if snmp["version"] == "3" and not snmp.get("user"):
            raise ConfigError(f"{d['name']}: SNMPv3 requiere 'user'")
        # En v1/v2c la "community" funciona como una contraseña en texto plano.
        snmp.setdefault("community", "public")

    # ---- Normalización de Telegram ----
    tg = cfg["telegram"]
    ids = tg.get("chat_ids") or []
    # chat_ids puede venir como lista, como un solo número, o como texto "123,456" (desde .env).
    if isinstance(ids, (str, int)):
        ids = str(ids).split(",")
    # Se convierten todos a enteros, ignorando elementos vacíos.
    tg["chat_ids"] = [int(str(c).strip()) for c in ids if str(c).strip()]
    # Un token real de bot tiene el formato "123456:ABC...". Si falta el ":", si todavía es el
    # texto de ejemplo ("xxxx") o no hay chats, Telegram se desactiva en vez de fallar luego.
    if tg["enabled"] and (":" not in str(tg["bot_token"]) or "xxxx" in tg["bot_token"] or not tg["chat_ids"]):
        print("[config] Telegram habilitado pero sin token/chat_id válidos -> se desactiva")
        tg["enabled"] = False
    return cfg
