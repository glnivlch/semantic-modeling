"""Демонстрационный граф Н-101 из раздела 8 договора данных версии 0.3."""

import argparse
from pathlib import Path
import sys

from graph_core import GraphCore, GraphInitializationError


_NODES = (
    {
        "id": "equipment:n101",
        "type": "Equipment",
        "label": "Насос Н-101",
        "properties": {"equipment_kind": "pump"},
    },
    {
        "id": "component:m101",
        "type": "Component",
        "label": "Электродвигатель М-101",
        "properties": {"component_kind": "motor"},
    },
    {
        "id": "sensor:t101",
        "type": "Sensor",
        "label": "Датчик температуры Т-101",
        "properties": {"quantity": "temperature"},
    },
    {
        "id": "measurement:t001",
        "type": "Measurement",
        "label": "Температура 95 °C",
        "properties": {
            "quantity": "temperature",
            "value": 95,
            "unit": "°C",
            "observed_at": "2026-11-10T10:00:00+05:00",
        },
    },
    {
        "id": "measurement:t002",
        "type": "Measurement",
        "label": "Температура 96 °C",
        "properties": {
            "quantity": "temperature",
            "value": 96,
            "unit": "°C",
            "observed_at": "2026-11-10T10:01:00+05:00",
        },
    },
    {
        "id": "event:overheat-001",
        "type": "Event",
        "label": "Перегрев двигателя",
        "properties": {
            "event_kind": "overheating",
            "occurred_at": "2026-11-10T10:00:00+05:00",
        },
    },
    {
        "id": "event:stop-001",
        "type": "Event",
        "label": "Остановка насоса",
        "properties": {
            "event_kind": "shutdown",
            "occurred_at": "2026-11-10T10:02:00+05:00",
        },
    },
)

# ID, начало, отношение, конец.
_EDGES = (
    ("edge:001", "equipment:n101", "HAS_COMPONENT", "component:m101"),
    ("edge:002", "sensor:t101", "INSTALLED_ON", "component:m101"),
    ("edge:002b", "sensor:t101", "MONITORS", "component:m101"),
    ("edge:003", "sensor:t101", "PRODUCED_MEASUREMENT", "measurement:t001"),
    ("edge:004", "measurement:t001", "MEASURES", "component:m101"),
    ("edge:005", "sensor:t101", "PRODUCED_MEASUREMENT", "measurement:t002"),
    ("edge:006", "measurement:t002", "MEASURES", "component:m101"),
    ("edge:007", "component:m101", "HAS_EVENT", "event:overheat-001"),
    ("edge:008", "equipment:n101", "HAS_EVENT", "event:stop-001"),
)


def create_demo_graph(graph: GraphCore) -> dict:
    """Загрузить набор Н-101 и вернуть его записи или первую ошибку ядра.

    Эффект created означает, что добавлена хотя бы одна запись.
    GraphCore сохраняет записи по одной, поэтому загрузку можно повторить.
    """
    edges = (
        {"id": edge_id, "source": source, "relation": relation, "target": target}
        for edge_id, source, relation, target in _EDGES
    )
    summary = {"status": "ok", "nodes": [], "edges": [], "effect": "already_exists"}
    for key, records, create in (
        ("nodes", _NODES, graph.create_node),
        ("edges", edges, graph.create_edge),
    ):
        for record in records:
            result = create(record)
            if result["status"] == "error":
                return {**result, "message": f"{record['id']} — {result['message']}"}
            summary[key].extend(result[key])
            if result["effect"] == "created":
                summary["effect"] = "created"
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузить демонстрационный граф Н-101.")
    parser.add_argument(
        "--db", type=Path, default=Path("data/demo.sqlite3"),
        help="путь к базе SQLite (по умолчанию data/demo.sqlite3)",
    )
    db_path = parser.parse_args(argv).db
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        graph = GraphCore(db_path)
    except OSError as exc:
        print(f"Ошибка хранилища (storage_error) — {exc}", file=sys.stderr)
        return 1
    except GraphInitializationError as exc:
        print(f"Ошибка открытия графа ({exc.error_code}) — {exc}", file=sys.stderr)
        return 1

    try:
        result = create_demo_graph(graph)
        saved = graph.save_graph() if result["status"] == "ok" else result
    finally:
        graph.close()

    if saved["status"] == "error":
        print(f"Ошибка ({saved['error_code']}) — {saved['message']}", file=sys.stderr)
        return 1

    print(f"Количество вершин: {saved['saved_node_count']}")
    print(f"Количество связей: {saved['saved_edge_count']}")
    if result["effect"] == "created":
        print("Демонстрационные данные успешно созданы.")
    else:
        print("Демонстрационные данные успешно проверены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
