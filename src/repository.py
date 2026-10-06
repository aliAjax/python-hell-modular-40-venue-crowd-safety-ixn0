import contextlib
import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS source_registry (
                    venue_id TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    incident_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    merge_record_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(venue_id, source_ref)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    # ---- 事件归并：来源登记表 ---------------------------------------------

    def register_source(self, venue_id, source_ref, incident_id, channel):
        now = utcnow()
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO source_registry(venue_id, source_ref, incident_id, channel, "
                    "status, merge_record_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)",
                    (venue_id, source_ref, incident_id, channel, now, now),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(
                    "source already registered: %s:%s" % (venue_id, source_ref)
                )
        return self.get_source(venue_id, source_ref)

    def get_source(self, venue_id, source_ref):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_registry WHERE venue_id = ? AND source_ref = ?",
                (venue_id, source_ref),
            ).fetchone()
        return self._source_from_row(row) if row else None

    def list_sources(self, venue_id=None, status=None):
        clauses = []
        params = []
        if venue_id:
            clauses.append("venue_id = ?")
            params.append(venue_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_registry" + where + " ORDER BY created_at, incident_id",
                params,
            ).fetchall()
        return [self._source_from_row(row) for row in rows]

    @staticmethod
    def _source_from_row(row):
        return {
            "venue_id": row["venue_id"],
            "source_ref": row["source_ref"],
            "incident_id": row["incident_id"],
            "channel": row["channel"],
            "status": row["status"],
            "merge_record_id": row["merge_record_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @contextlib.contextmanager
    def unit_of_work(self):
        """单连接事务，供归并/撤销原子改写多个实体。"""
        connection = self._connect()
        uow = UnitOfWork(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield uow
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class UnitOfWork:
    """一次事务内对实体、来源登记、审计和幂等键的全部读写。"""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _entity_from_row(row):
        return SQLiteRepository._entity_from_row(row)

    @staticmethod
    def _source_from_row(row):
        return SQLiteRepository._source_from_row(row)

    def get_entity(self, entity_id):
        row = self.connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_by_field(self, kind, field, value):
        rows = self.connection.execute(
            "SELECT * FROM entities WHERE kind = ? ORDER BY created_at, id", (kind,)
        ).fetchall()
        entities = [self._entity_from_row(row) for row in rows]
        if field == "id":
            return [entity for entity in entities if entity["id"] == value]
        return [entity for entity in entities if entity["data"].get(field) == value]

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        self.connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (
                entity_id,
                kind,
                status,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                actor_id,
                now,
                now,
            ),
        )
        return self.get_entity(entity_id)

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        row = self.connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        self.connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ?",
            (
                status,
                json.dumps(data, ensure_ascii=False, sort_keys=True),
                now,
                entity_id,
            ),
        )
        return self.get_entity(entity_id)

    def get_source(self, venue_id, source_ref):
        row = self.connection.execute(
            "SELECT * FROM source_registry WHERE venue_id = ? AND source_ref = ?",
            (venue_id, source_ref),
        ).fetchone()
        return self._source_from_row(row) if row else None

    def register_source(self, venue_id, source_ref, incident_id, channel):
        now = utcnow()
        try:
            self.connection.execute(
                "INSERT INTO source_registry(venue_id, source_ref, incident_id, channel, "
                "status, merge_record_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)",
                (venue_id, source_ref, incident_id, channel, now, now),
            )
        except sqlite3.IntegrityError:
            raise ConflictError(
                "source already registered: %s:%s" % (venue_id, source_ref)
            )
        return self.get_source(venue_id, source_ref)

    def mark_source(self, venue_id, source_ref, status, merge_record_id):
        self.connection.execute(
            "UPDATE source_registry SET status = ?, merge_record_id = ?, updated_at = ? "
            "WHERE venue_id = ? AND source_ref = ?",
            (status, merge_record_id, utcnow(), venue_id, source_ref),
        )

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        self.connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def save_idempotency(self, actor_id, idem_key, entity_id):
        self.connection.execute(
            "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (actor_id, idem_key, entity_id, utcnow()),
        )

    def get_idempotency(self, actor_id, idem_key):
        row = self.connection.execute(
            "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
            (actor_id, idem_key),
        ).fetchone()
        return row["entity_id"] if row else None
