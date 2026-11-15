"""Интеграционные проверки SQLite и восстановления GraphCore."""

import sqlite3

import pytest

from graph_core import GraphCore, GraphInitializationError
import sqlite_storage as storage


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "graph.sqlite3"


@pytest.fixture
def core(db_path):
    graph = GraphCore(db_path)
    yield graph
    graph.close()


@pytest.fixture
def pump():
    return {"id": "equipment:n101", "type": "Equipment", "label": "Насос Н-101",
            "properties": {"details": [{"values": [None, True, 95.5, "Данные"]}]},
            "aliases": ["Помпа"], "extra": {"tags": ["demo"]}}


@pytest.fixture
def populated(core, pump):
    assert core.create_node(pump)["effect"] == "created"
    core.create_node({"id": "component:m101", "type": "Component", "label": "Двигатель"})
    edge = {"id": "edge:001", "source": pump["id"], "target": "component:m101",
            "relation": "HAS_COMPONENT", "properties": {"details": [{"tags": ["original"]}]}}
    assert core.create_edge(edge)["effect"] == "created"
    return core


def counts(db_path):
    connection = sqlite3.connect(db_path)
    try:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("nodes", "edges"))
    finally:
        connection.close()


def test_create_new_database(core, db_path):
    assert db_path.exists()
    assert counts(db_path) == (0, 0)
    assert core._connection.execute("PRAGMA user_version").fetchone() == (1,)
    assert core._connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    assert core.find_nodes()["status"] == "no_data"


def test_reopen_preserves_all_attributes_and_ids(populated, db_path):
    expected_nodes = populated.find_nodes()
    expected_neighbors = populated.neighbors("equipment:n101")
    populated.close()
    restored = GraphCore(db_path)
    try:
        assert restored.find_nodes() == expected_nodes
        assert restored.neighbors("equipment:n101") == expected_neighbors
        assert restored._edge_locations == {"edge:001": ("equipment:n101", "component:m101")}
        assert restored.create_edge(expected_neighbors["edges"][0])["effect"] == "already_exists"
    finally:
        restored.close()


def test_parallel_edges_survive_restarts(populated, db_path):
    first = populated.neighbors("equipment:n101")["edges"][0]
    second = {**first, "id": "edge:002"}
    populated.create_edge(second)
    populated.close()
    for _ in range(2):
        restored = GraphCore(db_path)
        try:
            assert restored.neighbors("equipment:n101")["edges"] == [first, second]
            assert restored.create_edge(second)["effect"] == "already_exists"
        finally:
            restored.close()
    assert counts(db_path) == (2, 2)


def test_creates_are_committed_immediately(populated, db_path):
    # Другое соединение видит записи до save_graph и close.
    assert counts(db_path) == (2, 1)


def test_repeated_creates_and_save_do_not_duplicate(populated, pump, db_path):
    edge = populated.neighbors(pump["id"])["edges"][0]
    for _ in range(2):
        assert populated.create_node(pump)["effect"] == "already_exists"
        assert populated.create_edge(edge)["effect"] == "already_exists"
        assert populated.save_graph() == {
            "status": "ok", "nodes": [], "edges": [], "effect": "saved",
            "saved_node_count": 2, "saved_edge_count": 1,
        }
    assert counts(db_path) == (2, 1)


def test_empty_graph_can_be_saved(core, db_path):
    assert core.save_graph()["saved_node_count"] == 0
    core.close()
    reopened = GraphCore(db_path)
    try:
        assert reopened.find_nodes()["status"] == "no_data"
    finally:
        reopened.close()


@pytest.mark.parametrize("content", [b"not a sqlite database", b""])
def test_invalid_existing_file_is_not_overwritten(db_path, content):
    db_path.write_bytes(content)
    with pytest.raises(GraphInitializationError) as error:
        GraphCore(db_path)
    assert error.value.error_code == "storage_error"
    assert error.value.message
    assert db_path.read_bytes() == content


def test_unknown_version_is_not_overwritten(populated, db_path):
    populated._connection.execute("PRAGMA user_version = 99")
    populated.close()
    original = db_path.read_bytes()
    with pytest.raises(GraphInitializationError, match="версия") as error:
        GraphCore(db_path)
    assert error.value.error_code == "storage_error"
    assert db_path.read_bytes() == original


def test_incorrect_schema_is_rejected(db_path):
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    original = db_path.read_bytes()
    with pytest.raises(GraphInitializationError):
        GraphCore(db_path)
    assert db_path.read_bytes() == original


def test_foreign_keys_are_enforced(core):
    with pytest.raises(sqlite3.IntegrityError):
        storage.write_edge(core._connection, {
            "id": "edge:orphan", "source": "missing", "target": "also-missing",
            "relation": "HAS_COMPONENT",
        })
    assert not core._connection.in_transaction


def test_orphan_edge_is_rejected_on_load(core, db_path):
    core._connection.execute("PRAGMA foreign_keys = OFF")
    core._connection.execute(
        "INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
        ("edge:orphan", "missing", "also-missing", "HAS_COMPONENT", "{}"),
    )
    core.close()
    original = db_path.read_bytes()
    with pytest.raises(GraphInitializationError) as error:
        GraphCore(db_path)
    assert error.value.error_code == "invalid_data"
    assert db_path.read_bytes() == original


@pytest.mark.parametrize("encoded", ["not-json", "[]", '{"x": NaN}', '{"x": Infinity}',
                                     '{"x": 1e999}', '{"id": "shadow"}'])
def test_invalid_json_is_rejected_without_rewriting(core, db_path, pump, encoded):
    core.create_node(pump)
    core._connection.execute("UPDATE nodes SET attributes_json = ? WHERE id = ?",
                             (encoded, pump["id"]))
    core.close()
    original = db_path.read_bytes()
    with pytest.raises(GraphInitializationError) as error:
        GraphCore(db_path)
    assert error.value.error_code == "invalid_data"
    assert db_path.read_bytes() == original


@pytest.mark.parametrize("field, value", [("type", "Unknown"), ("label", "")])
def test_loaded_nodes_are_validated(core, db_path, pump, field, value):
    core.create_node(pump)
    # Игнорирование CHECK моделирует некорректные данные стороннего источника.
    core._connection.execute("PRAGMA ignore_check_constraints = ON")
    core._connection.execute(f"UPDATE nodes SET {field} = ?", (value,))
    core.close()
    with pytest.raises(GraphInitializationError) as error:
        GraphCore(db_path)
    assert error.value.error_code == "invalid_data"


def test_cross_table_id_collision_is_rejected_on_load(populated, db_path):
    populated._connection.execute("UPDATE edges SET id = ?", ("equipment:n101",))
    populated.close()
    with pytest.raises(GraphInitializationError) as error:
        GraphCore(db_path)
    assert error.value.error_code == "invalid_data"


def test_sql_failure_keeps_memory_and_database_unchanged(core, pump, db_path):
    core._connection.execute("""CREATE TRIGGER reject_node BEFORE INSERT ON nodes
        BEGIN SELECT RAISE(ABORT, 'test write failure'); END""")
    result = core.create_node(pump)
    assert result["error_code"] == "storage_error"
    assert core.find_nodes()["status"] == "no_data"
    assert counts(db_path) == (0, 0)
    assert not core._connection.in_transaction
    core._connection.execute("DROP TRIGGER reject_node")
    assert core.create_node(pump)["effect"] == "created"


def test_commit_failure_rolls_back_all_statements(core, pump, db_path):
    core._connection.execute("""CREATE TRIGGER orphan_after_insert AFTER INSERT ON nodes
        BEGIN INSERT INTO edges VALUES ('edge:bad', 'missing', NEW.id, 'HAS_COMPONENT', '{}'); END""")
    # Нарушение FK будет обнаружено именно при commit, не при INSERT.
    core._connection.execute("PRAGMA defer_foreign_keys = ON")
    result = core.create_node(pump)
    assert result["error_code"] == "storage_error"
    assert counts(db_path) == (0, 0)
    assert core.find_nodes()["status"] == "no_data"
    assert not core._connection.in_transaction


def test_edge_write_failure_keeps_graph_unchanged(populated, db_path):
    before = populated.neighbors("equipment:n101")
    populated._connection.execute("""CREATE TRIGGER reject_edge BEFORE INSERT ON edges
        BEGIN SELECT RAISE(ABORT, 'test edge failure'); END""")
    edge = {**before["edges"][0], "id": "edge:002"}
    assert populated.create_edge(edge)["error_code"] == "storage_error"
    assert populated.neighbors("equipment:n101") == before
    assert "edge:002" not in populated._edge_locations
    assert counts(db_path) == (2, 1)


def test_save_graph_rolls_back_partial_snapshot(core, db_path):
    # Моделируем подготовленный снимок, чтобы ошибка произошла после первой записи.
    for node_id in ("a", "b"):
        core._add_node_to_graph({"id": node_id, "type": "Equipment", "label": node_id,
                                 "properties": {}})
    core._connection.execute("""CREATE TRIGGER reject_b BEFORE INSERT ON nodes
        WHEN NEW.id = 'b' BEGIN SELECT RAISE(ABORT, 'test second insert'); END""")
    before = core.find_nodes()
    assert core.save_graph()["error_code"] == "storage_error"
    assert core.find_nodes() == before
    assert counts(db_path) == (0, 0)


def test_save_graph_does_not_overwrite_changed_facts(populated, db_path):
    populated._graph.nodes["equipment:n101"]["label"] = "Изменено напрямую"
    assert populated.save_graph()["error_code"] == "invalid_data"
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT label FROM nodes WHERE id = ?",
                                  ("equipment:n101",)).fetchone() == ("Насос Н-101",)
    finally:
        connection.close()


def test_save_graph_checks_edge_index(populated):
    populated._edge_locations.clear()
    assert populated.save_graph()["error_code"] == "invalid_data"


def test_memory_instances_are_isolated(pump):
    first, second = GraphCore(), GraphCore()
    try:
        first.create_node(pump)
        assert second.find_nodes()["status"] == "no_data"
        assert first.save_graph()["saved_node_count"] == 1
        assert second.save_graph()["saved_node_count"] == 0
    finally:
        first.close()
        second.close()


def test_close_is_idempotent_and_operations_are_blocked(core, pump):
    core.close()
    core.close()
    for result in (core.create_node(pump), core.find_nodes(), core.create_edge({}),
                   core.neighbors("missing"), core.save_graph()):
        assert result["error_code"] == "storage_error"


def test_memory_update_failure_recovers_from_committed_data(core, pump, monkeypatch, db_path):
    def fail(record):
        raise RuntimeError("test memory update failure")
    monkeypatch.setattr(core, "_add_node_to_graph", fail)
    assert core.create_node(pump)["effect"] == "created"
    assert core.find_nodes(id=pump["id"])["status"] == "ok"
    assert counts(db_path) == (1, 0)


def test_failed_recovery_blocks_operations(core, pump, monkeypatch, db_path):
    def fail(*args):
        raise RuntimeError("test recovery failure")
    monkeypatch.setattr(core, "_add_node_to_graph", fail)
    monkeypatch.setattr(core, "_restore", fail)
    assert core.create_node(pump)["error_code"] == "storage_error"
    assert counts(db_path) == (1, 0)
    assert core.find_nodes()["error_code"] == "storage_error"
    assert core.save_graph()["error_code"] == "storage_error"


def test_edge_memory_failure_recovers_index(populated, monkeypatch):
    edge = {**populated.neighbors("equipment:n101")["edges"][0], "id": "edge:002"}
    def fail(record):
        raise RuntimeError("test edge memory failure")
    monkeypatch.setattr(populated, "_add_edge_to_graph", fail)
    assert populated.create_edge(edge)["effect"] == "created"
    assert len(populated.neighbors("equipment:n101")["edges"]) == 2
    assert populated.create_edge(edge)["effect"] == "already_exists"


def test_loading_does_not_call_public_creation_methods(populated, db_path, monkeypatch):
    populated.close()
    def fail(*args):
        raise AssertionError("Загрузка не должна создавать факты повторно.")
    monkeypatch.setattr(GraphCore, "create_node", fail)
    monkeypatch.setattr(GraphCore, "create_edge", fail)
    restored = GraphCore(db_path)
    try:
        assert len(restored.find_nodes()["nodes"]) == 2
        assert len(restored.neighbors("equipment:n101")["edges"]) == 1
    finally:
        restored.close()


@pytest.mark.parametrize("db_path", [None, "", 123])
def test_invalid_database_path_is_reported(db_path):
    with pytest.raises(GraphInitializationError) as error:
        GraphCore(db_path)
    assert error.value.error_code == "invalid_data"


def test_valid_node_without_properties_can_be_loaded_and_saved(core, db_path):
    core._connection.execute("INSERT INTO nodes VALUES (?, ?, ?, ?)",
                             ("equipment:n101", "Equipment", "Насос Н-101", "{}"))
    core.close()
    restored = GraphCore(db_path)
    try:
        assert restored.find_nodes()["nodes"][0]["properties"] == {}
        assert restored.save_graph()["effect"] == "saved"
    finally:
        restored.close()


def test_serialization_failure_is_reported_without_changes(core, pump, monkeypatch, db_path):
    def fail(*args, **kwargs):
        raise ValueError("test JSON serialization failure")
    monkeypatch.setattr(storage.json, "dumps", fail)
    assert core.create_node(pump)["error_code"] == "invalid_data"
    assert core.find_nodes()["status"] == "no_data"
    assert counts(db_path) == (0, 0)
    assert not core._connection.in_transaction


def test_reloaded_measurement_rejects_second_sensor(core, db_path):
    for sensor_id in ("sensor:t101", "sensor:t102"):
        core.create_node({"id": sensor_id, "type": "Sensor", "label": sensor_id,
                          "properties": {"quantity": "temperature"}})
    core.create_node({"id": "measurement:t001", "type": "Measurement", "label": "95 °C",
                      "properties": {"quantity": "temperature", "value": 95, "unit": "°C",
                                     "observed_at": "2026-11-10T10:00:00+05:00"}})
    first = {"id": "edge:001", "source": "sensor:t101", "target": "measurement:t001",
             "relation": "PRODUCED_MEASUREMENT"}
    assert core.create_edge(first)["effect"] == "created"
    core.close()
    restored = GraphCore(db_path)
    try:
        second = {**first, "id": "edge:002", "source": "sensor:t102"}
        assert restored.create_edge(second)["error_code"] == "conflict"
    finally:
        restored.close()


def test_inconsistent_measurement_relations_are_rejected_on_load(core, db_path):
    for target_id in ("equipment:n101", "equipment:n102"):
        core.create_node({"id": target_id, "type": "Equipment", "label": target_id})
    core.create_node({"id": "measurement:t001", "type": "Measurement", "label": "95 °C",
                      "properties": {"quantity": "temperature", "value": 95, "unit": "°C",
                                     "observed_at": "2026-11-10T10:00:00+05:00"}})
    for edge_id, target_id in (("edge:001", "equipment:n101"), ("edge:002", "equipment:n102")):
        core._connection.execute("INSERT INTO edges VALUES (?, ?, ?, ?, ?)",
                                 (edge_id, "measurement:t001", target_id, "MEASURES", "{}"))
    core.close()
    with pytest.raises(GraphInitializationError) as error:
        GraphCore(db_path)
    assert error.value.error_code == "invalid_data"
