"""Проверки создания, поиска и независимости данных ядра."""

from copy import deepcopy

import pytest

from graph_core import GraphCore


@pytest.fixture
def equipment():
    return {"id": "equipment:n101", "type": "Equipment", "label": "Насос Н-101"}


@pytest.fixture
def core():
    return GraphCore()


def test_create_equipment(core, equipment):
    result = core.create_node(equipment)
    assert result == {
        "status": "ok",
        "nodes": [{**equipment, "properties": {}}],
        "edges": [],
        "effect": "created",
    }


@pytest.mark.parametrize("node_type", ["Unknown", "", None, []])
def test_invalid_node_type(core, equipment, node_type):
    equipment["type"] = node_type
    assert core.create_node(equipment)["error_code"] == "invalid_data"
    assert core.find_nodes()["status"] == "no_data"


@pytest.mark.parametrize("field", ["id", "type", "label"])
def test_missing_required_field(core, equipment, field):
    del equipment[field]
    assert core.create_node(equipment)["error_code"] == "invalid_data"


def test_identical_node_is_not_duplicated(core, equipment):
    core.create_node(equipment)
    result = core.create_node({**equipment, "properties": {}})
    assert result["status"] == "ok"
    assert result["effect"] == "already_exists"
    assert len(core.find_nodes()["nodes"]) == 1


def test_conflicting_id_preserves_original(core, equipment):
    core.create_node(equipment)
    result = core.create_node({**equipment, "label": "Другой насос"})
    assert result["error_code"] == "conflict"
    assert core.find_nodes(id=equipment["id"])["nodes"][0]["label"] == "Насос Н-101"


def test_find_by_id(core, equipment):
    core.create_node(equipment)
    assert core.find_nodes(id=equipment["id"])["nodes"][0]["id"] == equipment["id"]
    assert core.find_nodes(id="missing") == {
        "status": "no_data", "nodes": [], "edges": [], "message": "Вершины не найдены."
    }


def test_find_by_type(core, equipment):
    core.create_node(equipment)
    core.create_node({"id": "component:m101", "type": "Component", "label": "Двигатель"})
    result = core.find_nodes(type="Component")
    assert [node["id"] for node in result["nodes"]] == ["component:m101"]


@pytest.mark.parametrize("text", ["нАсОс", "Н-101", "пОМПа"])
def test_find_by_text_and_alias(core, equipment, text):
    core.create_node({**equipment, "aliases": ["Помпа"]})
    assert len(core.find_nodes(text=text)["nodes"]) == 1


def test_filters_are_combined_and_results_are_sorted(core, equipment):
    core.create_node(equipment)
    core.create_node({"id": "equipment:a", "type": "Equipment", "label": "Насос А"})
    assert [n["id"] for n in core.find_nodes(text="")["nodes"]] == [
        "equipment:a", "equipment:n101"
    ]
    assert len(core.find_nodes(type="Equipment", text="Н-101")["nodes"]) == 1
    assert core.find_nodes(type="Component", text="Н-101")["status"] == "no_data"


@pytest.mark.parametrize("filters", [{"unknown": "x"}, {"id": 1}, {"type": "Bad"}])
def test_invalid_filters(core, filters):
    assert core.find_nodes(**filters)["error_code"] == "invalid_data"


@pytest.mark.parametrize("result_source", ["created", "existing", "found"])
def test_results_are_independent_deep_copies(core, equipment, result_source):
    equipment["properties"] = {"details": {"tags": ["original"]}}
    equipment["aliases"] = ["Помпа"]
    created = core.create_node(equipment)
    original = deepcopy(created["nodes"][0])
    if result_source == "created":
        result = created
    elif result_source == "existing":
        result = core.create_node(equipment)
    else:
        result = core.find_nodes()
    returned = result["nodes"][0]
    returned["label"] = "Изменено"
    returned["properties"]["details"]["tags"].append("changed")
    returned["aliases"].append("Изменено")
    assert core.find_nodes()["nodes"] == [original]


def test_input_is_also_copied(core, equipment):
    equipment["properties"] = {"tags": ["original"]}
    core.create_node(equipment)
    equipment["properties"]["tags"].append("changed")
    assert core.find_nodes()["nodes"][0]["properties"] == {"tags": ["original"]}


@pytest.mark.parametrize(
    "node_type, properties",
    [
        ("Measurement", {"quantity": "temperature", "value": 95, "unit": "°C",
                         "observed_at": "2026-11-10T10:00:00+05:00"}),
        ("Event", {"event_kind": "shutdown", "occurred_at": "2026-11-10T10:02:00+05:00"}),
    ],
)
def test_measurements_and_events_without_edges(core, node_type, properties):
    node = {"id": "sample", "type": node_type, "label": "Пример", "properties": properties}
    result = core.create_node(node)
    assert result["effect"] == "created"
    for field in properties:
        invalid = deepcopy(node)
        invalid["id"] = "invalid"
        del invalid["properties"][field]
        assert core.create_node(invalid)["error_code"] == "invalid_data"


@pytest.mark.parametrize("value", ["95", True, None])
def test_measurement_requires_numeric_value(core, value):
    result = core.create_node({
        "id": "measurement:1", "type": "Measurement", "label": "Температура",
        "properties": {"quantity": "temperature", "value": value, "unit": "°C",
                       "observed_at": "2026-11-10T10:00:00+05:00"},
    })
    assert result["error_code"] == "invalid_data"


@pytest.mark.parametrize("date", ["not-a-date", "2026-11-10T10:00:00", None])
def test_event_requires_date_with_timezone(core, date):
    result = core.create_node({
        "id": "event:1", "type": "Event", "label": "Остановка",
        "properties": {"event_kind": "shutdown", "occurred_at": date},
    })
    assert result["error_code"] == "invalid_data"


def test_dates_are_normalized_to_utc(core):
    node = {"id": "event:1", "type": "Event", "label": "Остановка", "properties": {
        "event_kind": "shutdown", "occurred_at": "2026-11-10T10:00:00+05:00",
    }}
    result = core.create_node(node)
    assert result["nodes"][0]["properties"]["occurred_at"] == "2026-11-10T05:00:00+00:00"
    node["properties"]["occurred_at"] = "2026-11-10T05:00:00+00:00"
    assert core.create_node(node)["effect"] == "already_exists"


def test_event_cannot_end_before_start(core):
    result = core.create_node({
        "id": "event:1", "type": "Event", "label": "Ремонт", "properties": {
            "event_kind": "repair", "occurred_at": "2026-11-10T10:00:00+05:00",
            "ended_at": "2026-11-10T09:00:00+05:00",
        },
    })
    assert result["error_code"] == "invalid_data"


@pytest.mark.parametrize("node", [None, [], {}, {"id": "x", "type": "Equipment", "label": "x", "properties": []}])
def test_invalid_records_return_errors(core, node):
    assert core.create_node(node)["error_code"] == "invalid_data"


@pytest.mark.parametrize("value", [{1, 2}, frozenset({1}), (1, 2), b"data", object()])
@pytest.mark.parametrize("nested", [False, True])
def test_reject_non_json_types_anywhere(core, equipment, value, nested):
    if nested:
        equipment["properties"] = {"details": [{"value": value}]}
    else:
        equipment["extra"] = value
    result = core.create_node(equipment)
    assert result["status"] == "error"
    assert result["error_code"] == "invalid_data"
    assert result["nodes"] == []
    assert core.find_nodes()["status"] == "no_data"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("nested", [False, True])
def test_reject_non_finite_numbers_anywhere(core, equipment, value, nested):
    equipment["extra"] = {"readings": [value]} if nested else value
    result = core.create_node(equipment)
    assert result["status"] == "error"
    assert result["error_code"] == "invalid_data"
    assert core.find_nodes()["status"] == "no_data"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_reject_non_finite_measurement_value(core, value):
    result = core.create_node({
        "id": "measurement:1", "type": "Measurement", "label": "Температура",
        "properties": {"quantity": "temperature", "value": value, "unit": "°C",
                       "observed_at": "2026-11-10T10:00:00+05:00"},
    })
    assert result["status"] == "error"
    assert result["error_code"] == "invalid_data"


@pytest.mark.parametrize("key", [1, None, ("key",)])
def test_reject_non_string_dictionary_keys(core, equipment, key):
    equipment["properties"] = {"details": [{key: "value"}]}
    result = core.create_node(equipment)
    assert result["status"] == "error"
    assert result["error_code"] == "invalid_data"


@pytest.mark.parametrize("container", [{}, []])
def test_reject_circular_data(core, equipment, container):
    if isinstance(container, dict):
        container["self"] = container
    else:
        container.append(container)
    equipment["extra"] = container
    result = core.create_node(equipment)
    assert result["status"] == "error"
    assert result["error_code"] == "invalid_data"


def test_accept_nested_json_values(core, equipment):
    equipment["properties"] = {
        "details": [{"values": [None, True, False, 0, -1, 95.5, "Насос", [], {}]}]
    }
    original = deepcopy(equipment)
    result = core.create_node(equipment)
    assert result["effect"] == "created"
    assert result["nodes"] == [original]
    assert core.create_node(equipment)["effect"] == "already_exists"
    assert core.find_nodes(id=equipment["id"])["nodes"] == [original]
    assert equipment == original
