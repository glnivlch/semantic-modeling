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


@pytest.fixture
def demo(core, equipment):
    """Небольшой граф Н-101 без заранее созданных связей."""
    nodes = [
        equipment,
        {"id": "component:m101", "type": "Component", "label": "Двигатель М-101"},
        {"id": "sensor:t101", "type": "Sensor", "label": "Датчик Т-101",
         "properties": {"quantity": "temperature"}},
        {"id": "measurement:t001", "type": "Measurement", "label": "Температура 95 °C",
         "properties": {"quantity": "temperature", "value": 95, "unit": "°C",
                        "observed_at": "2026-11-10T10:00:00+05:00"}},
        {"id": "event:stop-001", "type": "Event", "label": "Остановка Н-101",
         "properties": {"event_kind": "shutdown",
                        "occurred_at": "2026-11-10T10:02:00+05:00"}},
    ]
    for node in nodes:
        assert core.create_node(node)["effect"] == "created"
    return core


@pytest.fixture
def edge():
    return {"id": "edge:002", "source": "sensor:t101",
            "target": "component:m101", "relation": "INSTALLED_ON"}


def test_create_edge(demo, edge):
    assert demo.create_edge(edge) == {
        "status": "ok", "nodes": [], "edges": [edge], "effect": "created"
    }


@pytest.mark.parametrize("field", ["id", "source", "target", "relation"])
@pytest.mark.parametrize("value", [None, "", [], 123])
def test_edge_requires_nonempty_string_fields(demo, edge, field, value):
    edge[field] = value
    result = demo.create_edge(edge)
    assert result["error_code"] == "invalid_data"
    assert demo.neighbors("sensor:t101")["status"] == "no_data"


@pytest.mark.parametrize("field", ["id", "source", "target", "relation"])
def test_edge_missing_field(demo, edge, field):
    del edge[field]
    assert demo.create_edge(edge)["error_code"] == "invalid_data"


@pytest.mark.parametrize("field", ["source", "target"])
def test_edge_missing_endpoint(demo, edge, field):
    edge[field] = "missing"
    assert demo.create_edge(edge)["error_code"] == "not_found"
    assert demo.find_nodes(id="missing")["status"] == "no_data"


@pytest.mark.parametrize("relation", ["UNKNOWN", "BEFORE", "AFTER"])
def test_reject_non_stored_relations(demo, edge, relation):
    edge["relation"] = relation
    assert demo.create_edge(edge)["error_code"] == "invalid_data"


@pytest.mark.parametrize("relation", ["CAUSES", "NOT_CAUSES"])
def test_deferred_relations_are_explicit(demo, edge, relation):
    edge["relation"] = relation
    assert demo.create_edge(edge)["error_code"] == "unsupported_operation"


@pytest.mark.parametrize("field", ["time_scope", "valid_from", "valid_to", "base_weight", "decay_policy"])
def test_deferred_edge_fields_are_not_ignored(demo, edge, field):
    edge[field] = "not-supported-yet"
    assert demo.create_edge(edge)["error_code"] == "unsupported_operation"


def test_edge_rejects_invalid_type_pair(demo, edge):
    edge["relation"] = "HAS_COMPONENT"
    assert demo.create_edge(edge)["error_code"] == "invalid_data"


@pytest.mark.parametrize("relation, source, target", [
    ("HAS_COMPONENT", "equipment:n101", "component:m101"),
    ("INSTALLED_ON", "sensor:t101", "equipment:n101"),
    ("INSTALLED_ON", "sensor:t101", "component:m101"),
    ("MONITORS", "sensor:t101", "equipment:n101"),
    ("MONITORS", "sensor:t101", "component:m101"),
    ("PRODUCED_MEASUREMENT", "sensor:t101", "measurement:t001"),
    ("MEASURES", "measurement:t001", "equipment:n101"),
    ("MEASURES", "measurement:t001", "component:m101"),
    ("HAS_EVENT", "equipment:n101", "event:stop-001"),
    ("HAS_EVENT", "component:m101", "event:stop-001"),
])
def test_all_allowed_edge_type_pairs(demo, relation, source, target):
    result = demo.create_edge({"id": "edge:test", "source": source,
                               "target": target, "relation": relation})
    assert result["effect"] == "created"


def test_repeated_edge(demo, edge):
    demo.create_edge(edge)
    assert demo.create_edge(edge)["effect"] == "already_exists"
    assert len(demo.neighbors(edge["source"])["edges"]) == 1


@pytest.mark.parametrize("changes", [
    {"relation": "MONITORS"}, {"target": "equipment:n101"},
    {"source": "measurement:t001", "target": "equipment:n101", "relation": "MEASURES"},
    {"properties": {"note": "changed"}},
])
def test_edge_id_conflict_is_global(demo, edge, changes):
    demo.create_edge(edge)
    assert demo.create_edge({**edge, **changes})["error_code"] == "conflict"
    assert demo.neighbors(edge["source"])["edges"] == [edge]


def test_edge_and_node_ids_cannot_collide(demo, edge):
    assert demo.create_edge({**edge, "id": "equipment:n101"})["error_code"] == "conflict"
    demo.create_edge(edge)
    result = demo.create_node({"id": edge["id"], "type": "Equipment", "label": "Насос"})
    assert result["error_code"] == "conflict"


def test_parallel_relations_and_facts_are_preserved(demo, edge):
    edges = [edge, {**edge, "id": "edge:003", "relation": "MONITORS"},
             {**edge, "id": "edge:001"}]
    for item in edges:
        assert demo.create_edge(item)["effect"] == "created"
    result = demo.neighbors(edge["source"])
    assert [n["id"] for n in result["nodes"]] == ["component:m101"]
    assert result["edges"] == sorted(edges, key=lambda item: item["id"])


@pytest.mark.parametrize("direction, subject", [
    ("outgoing", "sensor:t101"), ("incoming", "component:m101"),
])
def test_neighbors_direction(demo, edge, direction, subject):
    demo.create_edge(edge)
    result = demo.neighbors(subject, direction=direction)
    expected_neighbor = edge["target"] if direction == "outgoing" else edge["source"]
    assert [n["id"] for n in result["nodes"]] == [expected_neighbor]
    assert result["edges"] == [edge]


def test_neighbors_both_directions_and_sorting(demo, edge):
    demo.create_edge(edge)
    outgoing = {"id": "edge:001", "source": "component:m101",
                "target": "event:stop-001", "relation": "HAS_EVENT"}
    demo.create_edge(outgoing)
    result = demo.neighbors("component:m101", direction="both")
    assert [n["id"] for n in result["nodes"]] == ["event:stop-001", "sensor:t101"]
    assert result["edges"] == [outgoing, edge]


def test_neighbors_relation_filter(demo, edge):
    demo.create_edge(edge)
    demo.create_edge({**edge, "id": "edge:003", "relation": "MONITORS"})
    assert demo.neighbors(edge["source"], relation="INSTALLED_ON")["edges"] == [edge]
    assert demo.neighbors(edge["source"], relation="HAS_EVENT")["status"] == "no_data"


def test_neighbors_missing_node_and_empty_result(demo):
    assert demo.neighbors("missing")["error_code"] == "not_found"
    assert demo.neighbors("equipment:n101") == {
        "status": "no_data", "nodes": [], "edges": [], "message": "Соседние вершины не найдены."
    }


@pytest.mark.parametrize("filters", [
    {"direction": "bad"}, {"direction": []}, {"relation": []},
    {"relation": "UNKNOWN"}, {"relation": ""}, {"extra": "x"},
])
def test_neighbors_invalid_filters(demo, filters):
    assert demo.neighbors("sensor:t101", **filters)["error_code"] == "invalid_data"


@pytest.mark.parametrize("filters", [
    {"valid_at": "2026-11-10T10:00:00+05:00"}, {"relation": "BEFORE"},
    {"relation": "CAUSES"},
])
def test_neighbors_deferred_filters(demo, filters):
    assert demo.neighbors("sensor:t101", **filters)["error_code"] == "unsupported_operation"


@pytest.mark.parametrize("subject_id", [None, [], "", 123])
def test_neighbors_invalid_subject(demo, subject_id):
    assert demo.neighbors(subject_id)["error_code"] == "invalid_data"


def test_measurement_quantity_must_match_sensor(demo):
    demo.create_node({"id": "sensor:p101", "type": "Sensor", "label": "Датчик давления",
                      "properties": {"quantity": "pressure"}})
    result = demo.create_edge({"id": "edge:mismatch", "source": "sensor:p101",
                               "target": "measurement:t001", "relation": "PRODUCED_MEASUREMENT"})
    assert result["error_code"] == "invalid_data"
    assert demo.neighbors("sensor:p101")["status"] == "no_data"


def test_sensor_without_quantity_can_produce_measurement(demo):
    demo.create_node({"id": "sensor:unknown", "type": "Sensor", "label": "Датчик"})
    result = demo.create_edge({"id": "edge:reading", "source": "sensor:unknown",
                               "target": "measurement:t001", "relation": "PRODUCED_MEASUREMENT"})
    assert result["effect"] == "created"


@pytest.mark.parametrize("result_source", ["created", "existing", "neighbors"])
def test_edge_results_are_independent(demo, edge, result_source):
    edge["properties"] = {"details": {"tags": ["original"]}}
    original = deepcopy(edge)
    created = demo.create_edge(edge)
    if result_source == "created":
        result = created
    elif result_source == "existing":
        result = demo.create_edge(edge)
    else:
        result = demo.neighbors("sensor:t101")
        result["nodes"][0]["label"] = "Изменено"
    result["edges"][0]["properties"]["details"]["tags"].append("changed")
    edge["properties"]["details"]["tags"].append("input changed")
    found = demo.neighbors("sensor:t101")
    assert found["edges"] == [original]
    assert found["nodes"][0]["label"] == "Двигатель М-101"


@pytest.mark.parametrize("value", [{1}, float("nan"), float("inf"), float("-inf")])
def test_edge_rejects_non_json_values(demo, edge, value):
    edge["properties"] = {"nested": [value]}
    result = demo.create_edge(edge)
    assert result["status"] == "error"
    assert result["error_code"] == "invalid_data"


@pytest.mark.parametrize("edge_record", [None, [], {}])
def test_edge_requires_dictionary(demo, edge_record):
    assert demo.create_edge(edge_record)["error_code"] == "invalid_data"


@pytest.mark.parametrize("properties", [
    {"quantity": "temperature"}, {"quantity": "pressure"}, {},
])
def test_measurement_rejects_second_sensor(demo, properties):
    original = {"id": "edge:reading", "source": "sensor:t101",
                "target": "measurement:t001", "relation": "PRODUCED_MEASUREMENT"}
    assert demo.create_edge(original)["effect"] == "created"
    demo.create_node({"id": "sensor:t102", "type": "Sensor", "label": "Другой датчик",
                      "properties": properties})
    attempted = {**original, "id": "edge:other", "source": "sensor:t102"}
    result = demo.create_edge(attempted)
    assert result["status"] == "error"
    assert result["error_code"] == "conflict"
    assert demo.neighbors("measurement:t001", direction="incoming")["edges"] == [original]
    assert demo.neighbors("sensor:t102")["status"] == "no_data"
    # Неудачная попытка не занимает ID новой связи.
    assert demo.create_edge({**original, "id": "edge:other"})["effect"] == "created"


@pytest.mark.parametrize("first_target, second_target", [
    ("component:m101", "equipment:n101"),
    ("equipment:n101", "component:m101"),
])
def test_measurement_rejects_second_object(demo, first_target, second_target):
    original = {"id": "edge:object", "source": "measurement:t001",
                "target": first_target, "relation": "MEASURES"}
    assert demo.create_edge(original)["effect"] == "created"
    result = demo.create_edge({**original, "id": "edge:other", "target": second_target})
    assert result["status"] == "error"
    assert result["error_code"] == "conflict"
    assert demo.neighbors("measurement:t001")["edges"] == [original]
    assert demo.create_edge({**original, "id": "edge:other"})["effect"] == "created"


@pytest.mark.parametrize("relation, source, target, direction", [
    ("PRODUCED_MEASUREMENT", "sensor:t101", "measurement:t001", "incoming"),
    ("MEASURES", "measurement:t001", "component:m101", "outgoing"),
])
def test_measurement_allows_repeated_fact_and_parallel_edges(demo, relation, source, target, direction):
    first = {"id": "edge:fact-1", "source": source, "target": target, "relation": relation}
    second = {**first, "id": "edge:fact-2"}
    for record in (first, second):
        assert demo.create_edge(record)["effect"] == "created"
        repeated = demo.create_edge(record)
        assert repeated["status"] == "ok"
        assert repeated["effect"] == "already_exists"
    result = demo.neighbors("measurement:t001", relation=relation, direction=direction)
    assert result["edges"] == [first, second]
    assert len(result["nodes"]) == 1


def test_measurement_can_have_sensor_and_object_independently(demo):
    sensor_edge = {"id": "edge:sensor", "source": "sensor:t101",
                   "target": "measurement:t001", "relation": "PRODUCED_MEASUREMENT"}
    object_edge = {"id": "edge:object", "source": "measurement:t001",
                   "target": "component:m101", "relation": "MEASURES"}
    assert demo.create_edge(sensor_edge)["effect"] == "created"
    assert demo.create_edge(object_edge)["effect"] == "created"
    assert len(demo.neighbors("measurement:t001", direction="both")["edges"]) == 2


def test_measurement_without_edges_is_allowed(core):
    result = core.create_node({
        "id": "measurement:standalone", "type": "Measurement", "label": "Температура",
        "properties": {"quantity": "temperature", "value": 95, "unit": "°C",
                       "observed_at": "2026-11-10T10:00:00+05:00"},
    })
    assert result["effect"] == "created"
    assert core.neighbors("measurement:standalone", direction="both")["status"] == "no_data"


def test_empty_neighbors_message_does_not_change_find_nodes_message(demo, edge):
    demo.create_edge(edge)
    result = demo.neighbors("sensor:t101", relation="HAS_EVENT")
    assert result["status"] == "no_data"
    assert result["message"] == "Соседние вершины не найдены."
    assert demo.find_nodes(id="missing")["message"] == "Вершины не найдены."
