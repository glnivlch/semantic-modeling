"""SQLite-хранилище: структура базы, JSON и атомарная запись фактов."""

from contextlib import contextmanager
import json
from os import fspath
from pathlib import Path
import sqlite3


SCHEMA_VERSION = 1
NODE_FIELDS = ("id", "type", "label")
EDGE_FIELDS = ("id", "source", "target", "relation")
SCHEMA = (
    """CREATE TABLE nodes (
        id TEXT NOT NULL PRIMARY KEY CHECK(length(id) > 0),
        type TEXT NOT NULL CHECK(length(type) > 0),
        label TEXT NOT NULL CHECK(length(label) > 0),
        attributes_json TEXT NOT NULL
    )""",
    """CREATE TABLE edges (
        id TEXT NOT NULL PRIMARY KEY CHECK(length(id) > 0),
        source TEXT NOT NULL REFERENCES nodes(id),
        target TEXT NOT NULL REFERENCES nodes(id),
        relation TEXT NOT NULL CHECK(length(relation) > 0),
        attributes_json TEXT NOT NULL
    )""",
)


@contextmanager
def transaction(connection):
    """Откатить всю операцию, включая случай ошибки commit."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        try:
            connection.rollback()
        except sqlite3.Error:
            # Соединение с неизвестным состоянием больше не используется.
            connection.close()
            raise
        raise


def open_database(db_path):
    path = fspath(db_path)
    if not isinstance(path, str) or not path:
        raise ValueError("Путь к SQLite должен быть непустой строкой или Path.")
    is_new = path == ":memory:" or not Path(path).exists()
    # isolation_level=None: транзакции начинаются явно, а не неявно при INSERT.
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise sqlite3.DatabaseError("Не удалось включить внешние ключи.")
        if is_new:
            with transaction(connection):
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version = 1")
        validate_database(connection)
        return connection
    except BaseException:
        connection.close()
        raise


def validate_database(connection):
    """Не исправлять и не пересоздавать неподдерживаемое хранилище."""
    if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise sqlite3.DatabaseError("Неподдерживаемая версия структуры SQLite.")
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )}
    if tables != {"nodes", "edges"}:
        raise sqlite3.DatabaseError("Некорректный набор таблиц SQLite.")
    for table, fields in (("nodes", NODE_FIELDS), ("edges", EDGE_FIELDS)):
        # Имена таблиц здесь — внутренние константы, не пользовательские данные.
        columns = list(connection.execute(f"PRAGMA table_info({table})"))
        expected = [(name, "TEXT", 1, None, int(name == "id"))
                    for name in (*fields, "attributes_json")]
        if [(r[1], r[2], r[3], r[4], r[5]) for r in columns] != expected:
            raise sqlite3.DatabaseError(f"Некорректная структура таблицы {table}.")
        for index in connection.execute(f"PRAGMA index_list({table})"):
            if index[2] and index[3] != "pk":
                raise sqlite3.DatabaseError("Лишнее ограничение уникальности SQLite.")
    foreign_keys = list(connection.execute("PRAGMA foreign_key_list(edges)"))
    if len(foreign_keys) != 2 or {
        (r[2], r[3], r[4], r[5], r[6]) for r in foreign_keys
    } != {("nodes", field, "id", "NO ACTION", "NO ACTION") for field in ("source", "target")}:
        raise sqlite3.DatabaseError("Некорректные внешние ключи таблицы edges.")
    if list(connection.execute("PRAGMA foreign_key_list(nodes)")):
        raise sqlite3.DatabaseError("Лишние внешние ключи таблицы nodes.")
    integrity = [row[0] for row in connection.execute("PRAGMA quick_check")]
    if integrity != ["ok"]:
        if all(message.startswith(("CHECK constraint failed", "NULL value in"))
               for message in integrity):
            raise ValueError("Сохранённые записи нарушают ограничения обязательных полей.")
        raise sqlite3.DatabaseError("Нарушена целостность SQLite.")
    if list(connection.execute("PRAGMA foreign_key_check")):
        raise ValueError("Связь в SQLite ссылается на отсутствующую вершину.")


def _reject_constant(value):
    raise ValueError(f"Недопустимая JSON-константа: {value}.")


def read_graph(connection):
    validate_database(connection)
    result = []
    for table, fields in (("nodes", NODE_FIELDS), ("edges", EDGE_FIELDS)):
        records = []
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY id"):
            attributes = json.loads(row[-1], parse_constant=_reject_constant)
            if not isinstance(attributes, dict) or attributes.keys() & set(fields):
                raise ValueError("Некорректный набор дополнительных атрибутов SQLite.")
            record = {**dict(zip(fields, row[:-1])), **attributes}
            if table == "nodes":
                # По договору отсутствие properties равнозначно пустому словарю.
                record.setdefault("properties", {})
            records.append(record)
        result.append(records)
    return result[0], result[1]


def _insert(connection, table, fields, record):
    other_table = "edges" if table == "nodes" else "nodes"
    if connection.execute(f"SELECT 1 FROM {other_table} WHERE id = ?", (record["id"],)).fetchone():
        raise sqlite3.IntegrityError("ID уже занят записью другого типа.")
    attributes = {key: value for key, value in record.items() if key not in fields}
    encoded = json.dumps(attributes, ensure_ascii=False, allow_nan=False)
    values = [record[field] for field in fields] + [encoded]
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} VALUES ({placeholders})", values)


def write_node(connection, record):
    with transaction(connection):
        _insert(connection, "nodes", NODE_FIELDS, record)


def write_edge(connection, record):
    with transaction(connection):
        _insert(connection, "edges", EDGE_FIELDS, record)


def save_snapshot(connection, nodes, edges):
    """Дополнить проверенный снимок, не удаляя и не заменяя существующие факты."""
    with transaction(connection):
        saved_nodes, saved_edges = read_graph(connection)
        for table, fields, records, saved in (
            ("nodes", NODE_FIELDS, nodes, saved_nodes),
            ("edges", EDGE_FIELDS, edges, saved_edges),
        ):
            desired = {record["id"]: record for record in records}
            existing = {record["id"]: record for record in saved}
            for record_id, record in existing.items():
                if record_id not in desired or desired[record_id] != record:
                    raise ValueError("Текущий граф противоречит постоянным данным SQLite.")
            for record_id, record in desired.items():
                if record_id not in existing:
                    _insert(connection, table, fields, record)
