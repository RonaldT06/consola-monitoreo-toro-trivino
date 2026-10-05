"""Historial de eventos en SQLite.

Rol de este módulo en el sistema:
  Guarda de forma PERMANENTE (en un archivo en disco) todos los eventos que genera el monitor:
  equipos caídos o recuperados, interfaces, umbrales, traps... Así el historial sobrevive a un
  reinicio del programa y se puede consultar desde el dashboard.

  SQLite es una base de datos SQL completa contenida en UN solo archivo (ej. netmon.db). No
  necesita instalar ni administrar un servidor (como MySQL o PostgreSQL) y viene incluida en
  Python (módulo sqlite3).

  - Recibe: eventos (diccionarios) desde Monitor.emit().
  - Produce: listas de eventos recientes para la API web (/api/events) y el WebSocket.
"""
from __future__ import annotations

import sqlite3
import threading


class EventStore:
    """Acceso a la tabla "events" de la base de datos SQLite."""

    def __init__(self, path: str):
        """Abre (o crea) la base de datos y se asegura de que exista la tabla de eventos.

        Parámetros:
            path: ruta del archivo SQLite (general.db_path en la configuración).
        """
        # Candado (lock) para que dos accesos simultáneos no usen la conexión al mismo tiempo.
        self.lock = threading.Lock()
        # check_same_thread=False permite usar la conexión desde un hilo distinto al que la
        # creó (por defecto sqlite3 lo prohíbe). Hoy todo corre en el hilo de asyncio, pero
        # así el código queda preparado; el lock anterior garantiza que el acceso sea seguro.
        self.db = sqlite3.connect(path, check_same_thread=False)
        # "IF NOT EXISTS": la tabla solo se crea la primera vez; después se reutiliza.
        # Columnas: id autoincremental, ts = fecha/hora en segundos desde 1970 (epoch),
        # equipo, severidad, tipo de evento y mensaje.
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL, device TEXT, severity TEXT, kind TEXT, message TEXT)"""
        )
        # Índice sobre la columna ts: acelera las búsquedas y ordenamientos por fecha.
        self.db.execute("CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)")
        # commit() confirma los cambios en el archivo.
        self.db.commit()

    def add(self, ev: dict) -> int:
        """Inserta un evento en la base de datos.

        Parámetros:
            ev: evento con las claves ts, device, severity, kind y message.

        Retorna:
            El id (entero) que SQLite asignó al nuevo registro.
        """
        with self.lock:
            # Los "?" son parámetros: SQLite inserta los valores de forma segura, evitando
            # inyección SQL aunque el mensaje contenga comillas u otros caracteres especiales.
            cur = self.db.execute(
                "INSERT INTO events(ts, device, severity, kind, message) VALUES (?,?,?,?,?)",
                (ev["ts"], ev["device"], ev["severity"], ev["kind"], ev["message"]),
            )
            self.db.commit()
            return cur.lastrowid

    def recent(self, limit: int = 200, device: str | None = None) -> list[dict]:
        """Devuelve los eventos más recientes, opcionalmente filtrados por equipo.

        Parámetros:
            limit:  cantidad máxima de eventos a devolver.
            device: si se indica, solo los eventos de ese equipo.

        Retorna:
            Lista de diccionarios (id, ts, device, severity, kind, message), del más nuevo al
            más viejo.
        """
        # La consulta SQL se arma por partes según los filtros pedidos.
        q = "SELECT id, ts, device, severity, kind, message FROM events"
        args: list = []
        if device:
            q += " WHERE device = ?"
            args.append(device)
        # ORDER BY id DESC: los ids crecen con cada inserción, así que el mayor es el más nuevo.
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self.lock:
            rows = self.db.execute(q, args).fetchall()
        # Cada fila llega como tupla; zip la combina con los nombres de columna para formar un dict.
        return [dict(zip(("id", "ts", "device", "severity", "kind", "message"), r)) for r in rows]
