"""Проверки демонстрационного набора Н-101 и его запуска из терминала."""

from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from demo_data import create_demo_graph
from graph_core import GraphCore


@pytest.fixture
def graph():
    with closing(GraphCore()) as core:
        yield core


def snapshot(graph):
    nodes = graph.find_nodes()["nodes"]
    edges = [edge for node in nodes for edge in graph.neighbors(node["id"])["edges"]]
    return nodes, sorted(edges, key=lambda edge: edge["id"])


def test_creates_all_demo_nodes(graph):
    result = create_demo_graph(graph)
    assert (result["status"], result["effect"]) == ("ok", "created")
    nodes = graph.find_nodes()["nodes"]
    assert len(nodes) == 7
    assert {node["id"]: (node["type"], node["label"], node["properties"]) for node in nodes} == {
        "equipment:n101": ("Equipment", "Насос Н-101", {"equipment_kind": "pump"}),
        "component:m101": ("Component", "Электродвигатель М-101", {"component_kind": "motor"}),
        "sensor:t101": ("Sensor", "Датчик температуры Т-101", {"quantity": "temperature"}),
        "measurement:t001": ("Measurement", "Температура 95 °C", {
            "quantity": "temperature", "value": 95, "unit": "°C",
            "observed_at": "2026-11-10T05:00:00+00:00",
        }),
        "measurement:t002": ("Measurement", "Температура 96 °C", {
            "quantity": "temperature", "value": 96, "unit": "°C",
            "observed_at": "2026-11-10T05:01:00+00:00",
        }),
        "event:overheat-001": ("Event", "Перегрев двигателя", {
            "event_kind": "overheating", "occurred_at": "2026-11-10T05:00:00+00:00",
        }),
        "event:stop-001": ("Event", "Остановка насоса", {
            "event_kind": "shutdown", "occurred_at": "2026-11-10T05:02:00+00:00",
        }),
    }


def test_creates_all_demo_edges(graph):
    assert create_demo_graph(graph)["status"] == "ok"
    _, edges = snapshot(graph)
    assert [(edge["id"], edge["source"], edge["relation"], edge["target"]) for edge in edges] == [
        ("edge:001", "equipment:n101", "HAS_COMPONENT", "component:m101"),
        ("edge:002", "sensor:t101", "INSTALLED_ON", "component:m101"),
        ("edge:002b", "sensor:t101", "MONITORS", "component:m101"),
        ("edge:003", "sensor:t101", "PRODUCED_MEASUREMENT", "measurement:t001"),
        ("edge:004", "measurement:t001", "MEASURES", "component:m101"),
        ("edge:005", "sensor:t101", "PRODUCED_MEASUREMENT", "measurement:t002"),
        ("edge:006", "measurement:t002", "MEASURES", "component:m101"),
        ("edge:007", "component:m101", "HAS_EVENT", "event:overheat-001"),
        ("edge:008", "equipment:n101", "HAS_EVENT", "event:stop-001"),
    ]


def test_repeated_load_does_not_duplicate_facts(graph):
    first = create_demo_graph(graph)
    assert first["effect"] == "created"
    before = snapshot(graph)

    assert create_demo_graph(graph) == {**first, "effect": "already_exists"}
    assert snapshot(graph) == before


def test_conflicting_demo_id_is_reported(graph):
    pump = {"id": "equipment:n101", "type": "Equipment", "label": "Другой насос"}
    assert graph.create_node(pump)["effect"] == "created"

    result = create_demo_graph(graph)
    assert (result["status"], result["error_code"]) == ("error", "conflict")
    assert "equipment:n101" in result["message"]
    assert graph.find_nodes()["nodes"] == [{**pump, "properties": {}}]


def test_demo_data_survives_reopening_sqlite(tmp_path):
    db_path = tmp_path / "demo.sqlite3"
    with closing(GraphCore(db_path)) as graph:
        first = create_demo_graph(graph)
        assert first["effect"] == "created"
        expected = snapshot(graph)
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM nodes").fetchone() == (7,)
            assert connection.execute("SELECT COUNT(*) FROM edges").fetchone() == (9,)

    with closing(GraphCore(db_path)) as reopened:
        assert snapshot(reopened) == expected
        assert create_demo_graph(reopened) == {**first, "effect": "already_exists"}
        assert snapshot(reopened) == expected


def test_cli_creates_default_database_and_checks_it_on_second_run(tmp_path):
    script = Path(__file__).resolve().parents[1] / "src" / "demo_data.py"
    command = [sys.executable, "-X", "utf8", str(script)]

    for message in ("успешно созданы", "успешно проверены"):
        run = subprocess.run(command, cwd=tmp_path, capture_output=True, encoding="utf-8", check=True)
        assert "Количество вершин: 7" in run.stdout
        assert "Количество связей: 9" in run.stdout
        assert message in run.stdout
        assert (tmp_path / "data" / "demo.sqlite3").is_file()
