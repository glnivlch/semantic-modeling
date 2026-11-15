"""Минимальное ядро в памяти по договору данных версии 0.3.

SQLite хранит факты, NetworkX представляет проверенные данные в памяти.
"""

from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
import sqlite3

import networkx as nx

import sqlite_storage as storage


NODE_TYPES = {"Equipment", "Component", "Sensor", "Measurement", "Event"}
EDGE_TYPES = {
    "HAS_COMPONENT": {("Equipment", "Component")},
    "INSTALLED_ON": {("Sensor", "Equipment"), ("Sensor", "Component")},
    "MONITORS": {("Sensor", "Equipment"), ("Sensor", "Component")},
    "PRODUCED_MEASUREMENT": {("Sensor", "Measurement")},
    "MEASURES": {("Measurement", "Equipment"), ("Measurement", "Component")},
    "HAS_EVENT": {("Equipment", "Event"), ("Component", "Event")},
}
DEFERRED_RELATIONS = {"CAUSES", "NOT_CAUSES"}
COMPUTED_RELATIONS = {"BEFORE", "AFTER", "AFTER_NOT_CAUSED_BY"}
DEFERRED_EDGE_FIELDS = {"time_scope", "valid_from", "valid_to", "base_weight", "decay_policy"}


class GraphInitializationError(Exception):
    """Ошибка запуска: код договора и пояснение для пользователя."""

    def __init__(self, error_code, message):
        self.error_code = error_code
        self.message = message
        super().__init__(message)


class GraphCore:
    """Управляет вершинами и связями, сохраняя внутренний граф закрытым."""

    def __init__(self, db_path=":memory:"):
        self._graph = nx.MultiDiGraph()
        # Ключ ребра в NetworkX уникален только внутри пары вершин.
        # Этот индекс обеспечивает уникальность ID во всём графе.
        self._edge_locations = {}
        self._connection = None
        self._closed = False
        self._blocked = False
        try:
            self._connection = storage.open_database(db_path)
            self._restore()
        except (sqlite3.Error, OSError) as exc:
            self.close()
            raise GraphInitializationError("storage_error", str(exc)) from exc
        except (ValueError, TypeError, RecursionError, OverflowError) as exc:
            self.close()
            raise GraphInitializationError("invalid_data", str(exc)) from exc

    def create_node(self, node: dict) -> dict:
        """Проверить запись и добавить её либо вернуть результат повтора."""
        error = self._availability_error()
        if error:
            return error
        return self._create_node(node, persist=True)

    def _create_node(self, node, persist=False):
        """Общие проверки для создания и загрузки; загрузка не пишет в SQLite."""
        if not isinstance(node, dict):
            return self._error("invalid_data", "Вершина должна быть словарём.")

        # Проверяем исходные данные до копирования и изменения графа.
        try:
            compatible = self._is_json_compatible(node)
        except RecursionError:
            compatible = False
        if not compatible:
            return self._error(
                "invalid_data",
                "Запись должна содержать только JSON-совместимые значения: "
                "без NaN, бесконечностей и циклических ссылок.",
            )

        record = deepcopy(node)
        record.setdefault("properties", {})
        message = self._validate_node(record)
        if message:
            return self._error("invalid_data", message)

        node_id = record["id"]
        if node_id in self._edge_locations:
            return self._error("conflict", "ID уже занят связью.")
        if node_id in self._graph:
            existing = self._graph.nodes[node_id]
            if existing != record:
                return self._error("conflict", "ID уже занят другими данными.")
            return self._result([existing], effect="already_exists")

        if persist:
            try:
                storage.write_node(self._connection, record)
            except sqlite3.Error as exc:
                return self._storage_error(exc)
            except (ValueError, TypeError, RecursionError, OverflowError) as exc:
                return self._error("invalid_data", str(exc))
        try:
            self._add_node_to_graph(record)
        except Exception as exc:
            if not persist:
                raise
            error = self._recover_after_write(exc)
            if error:
                return error
        return self._result([record], effect="created")

    def find_nodes(self, **filters) -> dict:
        """Найти вершины: фильтры id, type и text объединяются через «и»."""
        error = self._availability_error()
        if error:
            return error
        if filters.keys() - {"id", "type", "text"}:
            return self._error("invalid_data", "Передан неподдерживаемый фильтр.")
        if any(not isinstance(value, str) for value in filters.values()):
            return self._error("invalid_data", "Значения фильтров должны быть строками.")
        if "type" in filters and filters["type"] not in NODE_TYPES:
            return self._error("invalid_data", "Неизвестный тип вершины.")

        found = []
        text = filters.get("text", "").casefold()
        for node_id in sorted(self._graph.nodes):
            record = self._graph.nodes[node_id]
            if "id" in filters and node_id != filters["id"]:
                continue
            if "type" in filters and record["type"] != filters["type"]:
                continue
            names = [record["label"], *record.get("aliases", [])]
            if text and not any(text in name.casefold() for name in names):
                continue
            found.append(record)

        return self._result(found)

    def create_edge(self, edge: dict) -> dict:
        """Создать сохраняемую связь, используя её ID как ключ ребра."""
        error = self._availability_error()
        if error:
            return error
        return self._create_edge(edge, persist=True)

    def _create_edge(self, edge, persist=False):
        if not isinstance(edge, dict):
            return self._error("invalid_data", "Связь должна быть словарём.")
        try:
            compatible = self._is_json_compatible(edge)
        except RecursionError:
            compatible = False
        if not compatible:
            return self._error("invalid_data", "Связь должна быть JSON-совместимой.")
        for field in ("id", "source", "target", "relation"):
            if not isinstance(edge.get(field), str) or not edge[field]:
                return self._error("invalid_data", f"Поле {field} должно быть непустой строкой.")
        if DEFERRED_EDGE_FIELDS.intersection(edge) or edge["relation"] in DEFERRED_RELATIONS:
            return self._error("unsupported_operation", "Временные, причинные отношения и старение пока не поддерживаются.")
        if edge["relation"] not in EDGE_TYPES:
            return self._error("invalid_data", "Неизвестное сохраняемое отношение.")
        if "properties" in edge and not isinstance(edge["properties"], dict):
            return self._error("invalid_data", "Поле properties должно быть словарём.")

        record = deepcopy(edge)
        try:
            if "created_at" in record:
                record["created_at"] = self._utc_time(record["created_at"])
        except (ValueError, TypeError):
            return self._error("invalid_data", "Дата должна быть строкой ISO 8601 с часовым поясом.")

        edge_id = record["id"]
        if edge_id in self._graph:
            return self._error("conflict", "ID уже занят вершиной.")
        if edge_id in self._edge_locations:
            source, target = self._edge_locations[edge_id]
            existing = self._graph.edges[source, target, edge_id]
            if existing != record:
                return self._error("conflict", "ID уже занят другими данными связи.")
            return self._result([], [existing], effect="already_exists")

        source, target = record["source"], record["target"]
        if source not in self._graph or target not in self._graph:
            return self._error("not_found", "Начальная или конечная вершина отсутствует.")
        source_node = self._graph.nodes[source]
        target_node = self._graph.nodes[target]
        relation = record["relation"]
        if (source_node["type"], target_node["type"]) not in EDGE_TYPES[relation]:
            return self._error("invalid_data", "Отношение не допускает выбранную пару типов вершин.")
        if relation == "PRODUCED_MEASUREMENT":
            # Сравниваем датчики, а не количество рёбер: параллельные факты допустимы.
            for sensor_id, _, existing in self._graph.in_edges(target, data=True):
                if existing["relation"] == relation and sensor_id != source:
                    return self._error("conflict", "Измерение уже связано с другим датчиком.")
            quantity = source_node["properties"].get("quantity")
            if "quantity" in source_node["properties"] and quantity != target_node["properties"]["quantity"]:
                return self._error("invalid_data", "Величина измерения не соответствует датчику.")
        if relation == "MEASURES":
            for _, object_id, existing in self._graph.out_edges(source, data=True):
                if existing["relation"] == relation and object_id != target:
                    return self._error("conflict", "Измерение уже связано с другим объектом.")

        if persist:
            try:
                storage.write_edge(self._connection, record)
            except sqlite3.Error as exc:
                return self._storage_error(exc)
            except (ValueError, TypeError, RecursionError, OverflowError) as exc:
                return self._error("invalid_data", str(exc))
        try:
            self._add_edge_to_graph(record)
        except Exception as exc:
            if not persist:
                raise
            error = self._recover_after_write(exc)
            if error:
                return error
        return self._result([], [record], effect="created")

    def neighbors(self, subject_id, relation=None, direction="outgoing", **filters) -> dict:
        """Вернуть соседей одного шага и все подходящие параллельные связи."""
        error = self._availability_error()
        if error:
            return error
        deferred_filters = {"valid_at", "event_time_from", "event_time_to",
                            "window_from", "window_to", "evaluation_time"}
        if filters.keys() - deferred_filters:
            return self._error("invalid_data", "Передан неподдерживаемый фильтр.")
        if filters:
            return self._error("unsupported_operation", "Расширенные условия пока не поддерживаются.")
        if not isinstance(subject_id, str) or not subject_id:
            return self._error("invalid_data", "subject_id должен быть непустой строкой.")
        if direction not in ("outgoing", "incoming", "both"):
            return self._error("invalid_data", "Некорректное направление поиска.")
        if relation is not None:
            if not isinstance(relation, str):
                return self._error("invalid_data", "Отношение должно быть строкой.")
            if relation in DEFERRED_RELATIONS or relation in COMPUTED_RELATIONS:
                return self._error("unsupported_operation", "Запрошенное отношение пока не поддерживается.")
            if relation not in EDGE_TYPES:
                return self._error("invalid_data", "Неизвестное сохраняемое отношение.")
        if subject_id not in self._graph:
            return self._error("not_found", "Исходная вершина отсутствует.")

        candidates = []
        if direction in ("outgoing", "both"):
            candidates.extend(self._graph.out_edges(subject_id, keys=True, data=True))
        if direction in ("incoming", "both"):
            candidates.extend(self._graph.in_edges(subject_id, keys=True, data=True))
        found_edges = {}
        neighbor_ids = set()
        for source, target, edge_id, record in candidates:
            if relation is not None and record["relation"] != relation:
                continue
            found_edges[edge_id] = record
            neighbor_ids.add(target if source == subject_id else source)
        nodes = [self._graph.nodes[node_id] for node_id in sorted(neighbor_ids)]
        edges = [found_edges[edge_id] for edge_id in sorted(found_edges)]
        result = self._result(nodes, edges)
        if result["status"] == "no_data":
            result["message"] = "Соседние вершины не найдены."
        return result

    def _add_node_to_graph(self, record):
        self._graph.add_node(record["id"])
        self._graph.nodes[record["id"]].update(record)

    def _add_edge_to_graph(self, record):
        source, target, edge_id = record["source"], record["target"], record["id"]
        self._graph.add_edge(source, target, key=edge_id)
        self._graph.edges[source, target, edge_id].update(record)
        self._edge_locations[edge_id] = (source, target)

    @classmethod
    def _build_graph(cls, nodes, edges):
        # Временный проверяющий объект без соединения и без публичных операций.
        candidate = cls.__new__(cls)
        candidate._graph = nx.MultiDiGraph()
        candidate._edge_locations = {}
        for records, add in ((nodes, candidate._create_node), (edges, candidate._create_edge)):
            for record in records:
                result = add(record, persist=False)
                if result.get("effect") != "created":
                    raise ValueError(result.get("message", "Повторяющиеся ID в снимке графа."))
        return candidate._graph, candidate._edge_locations

    def _restore(self):
        nodes, edges = storage.read_graph(self._connection)
        graph, locations = self._build_graph(nodes, edges)
        self._graph, self._edge_locations = graph, locations

    def _availability_error(self):
        if self._closed or self._blocked:
            return self._error("storage_error", "Ядро закрыто или заблокировано после ошибки хранения.")
        return None

    def _storage_error(self, exc):
        try:
            if self._connection.in_transaction:
                self._blocked = True
        except sqlite3.Error:
            self._blocked = True
        return self._error("storage_error", str(exc))

    def _recover_after_write(self, exc):
        """SQLite уже сохранила факт: перечитать его или запретить дальнейшую работу."""
        try:
            self._restore()
        except Exception:
            self._blocked = True
            return self._error("storage_error", f"Запись сохранена, но граф не восстановлен: {exc}")
        return None

    def save_graph(self) -> dict:
        """Проверить и атомарно сохранить снимок без удаления постоянных фактов."""
        error = self._availability_error()
        if error:
            return error
        try:
            nodes = []
            for node_id, record in self._graph.nodes(data=True):
                if node_id != record.get("id"):
                    raise ValueError("Ключ вершины не совпадает с её ID.")
                nodes.append(deepcopy(record))
            edges = []
            for source, target, edge_id, record in self._graph.edges(keys=True, data=True):
                if (source, target, edge_id) != (record.get("source"), record.get("target"), record.get("id")):
                    raise ValueError("Ключи связи не совпадают с её атрибутами.")
                edges.append(deepcopy(record))
            _, locations = self._build_graph(nodes, edges)
            if locations != self._edge_locations:
                raise ValueError("Индекс связей не соответствует графу.")
            storage.save_snapshot(self._connection, nodes, edges)
        except sqlite3.Error as exc:
            return self._storage_error(exc)
        except (ValueError, TypeError, RecursionError, OverflowError) as exc:
            return self._error("invalid_data", str(exc))
        return {"status": "ok", "nodes": [], "edges": [], "effect": "saved",
                "saved_node_count": len(nodes), "saved_edge_count": len(edges)}

    def close(self):
        """Закрыть соединение; повторный вызов ничего не меняет."""
        if not self._closed:
            if self._connection is not None:
                self._connection.close()
            self._closed = True

    @staticmethod
    def _is_json_compatible(value) -> bool:
        """Проверить простое значение и каждый элемент вложенных контейнеров."""
        if value is None or isinstance(value, (str, bool, int)):
            return True
        if isinstance(value, float):
            return isfinite(value)
        if isinstance(value, list):
            return all(GraphCore._is_json_compatible(item) for item in value)
        if isinstance(value, dict):
            return all(
                isinstance(key, str) and GraphCore._is_json_compatible(item)
                for key, item in value.items()
            )
        return False

    @staticmethod
    def _validate_node(record: dict) -> str | None:
        """Вернуть описание ошибки или None; даты нормализовать в UTC."""
        for field in ("id", "type", "label"):
            if not isinstance(record.get(field), str) or not record[field]:
                return f"Поле {field} должно быть непустой строкой."
        if record["type"] not in NODE_TYPES:
            return "Неизвестный тип вершины."
        if not isinstance(record["properties"], dict):
            return "Поле properties должно быть словарём."
        if "aliases" in record:
            aliases = record["aliases"]
            if not isinstance(aliases, list) or any(
                not isinstance(alias, str) for alias in aliases
            ):
                return "Поле aliases должно быть списком строк."

        properties = record["properties"]
        required = {
            "Measurement": ("quantity", "value", "unit", "observed_at"),
            "Event": ("event_kind", "occurred_at"),
        }.get(record["type"], ())
        for field in required:
            if field not in properties:
                return f"В properties отсутствует обязательное поле {field}."
            if field != "value" and (
                not isinstance(properties[field], str) or not properties[field]
            ):
                return f"Свойство {field} должно быть непустой строкой."
        if record["type"] == "Measurement":
            value = properties["value"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "Свойство value должно быть числом."

        date_fields = {
            "Measurement": ("observed_at",),
            "Event": ("occurred_at", "ended_at"),
        }.get(record["type"], ())
        try:
            for field in date_fields:
                if field in properties:
                    properties[field] = GraphCore._utc_time(properties[field])
            if "created_at" in record:
                record["created_at"] = GraphCore._utc_time(record["created_at"])
        except (ValueError, TypeError):
            return "Дата должна быть строкой ISO 8601 с часовым поясом."

        if record["type"] == "Event" and "ended_at" in properties:
            if datetime.fromisoformat(properties["ended_at"]) < datetime.fromisoformat(
                properties["occurred_at"]
            ):
                return "Время завершения не может быть раньше начала события."
        return None

    @staticmethod
    def _utc_time(value: str) -> str:
        if not isinstance(value, str):
            raise TypeError("Дата должна быть строкой.")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Для даты нужен часовой пояс.")
        return parsed.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _result(nodes: list, edges: list | None = None, **extra) -> dict:
        result = {
            "status": "ok" if nodes or edges else "no_data",
            "nodes": deepcopy(nodes),
            "edges": deepcopy(edges) if edges else [],
        }
        if not nodes and not edges:
            result["message"] = "Вершины не найдены."
        result.update(extra)
        return result

    @staticmethod
    def _error(code: str, message: str) -> dict:
        return {
            "status": "error",
            "nodes": [],
            "edges": [],
            "error_code": code,
            "message": message,
        }
