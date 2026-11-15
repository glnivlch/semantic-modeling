"""Минимальное ядро в памяти по договору данных версии 0.3.

SQLite и операции со связями будут добавлены на следующих этапах.
"""

from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite

import networkx as nx


NODE_TYPES = {"Equipment", "Component", "Sensor", "Measurement", "Event"}


class GraphCore:
    """Создаёт и ищет вершины, сохраняя внутренний граф закрытым."""

    def __init__(self):
        self._graph = nx.MultiDiGraph()

    def create_node(self, node: dict) -> dict:
        """Проверить запись и добавить её либо вернуть результат повтора."""
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
        if node_id in self._graph:
            existing = self._graph.nodes[node_id]
            if existing != record:
                return self._error("conflict", "ID уже занят другими данными.")
            return self._result([existing], effect="already_exists")

        self._graph.add_node(node_id)
        self._graph.nodes[node_id].update(record)
        return self._result([record], effect="created")

    def find_nodes(self, **filters) -> dict:
        """Найти вершины: фильтры id, type и text объединяются через «и»."""
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
    def _result(nodes: list, **extra) -> dict:
        result = {
            "status": "ok" if nodes else "no_data",
            "nodes": deepcopy(nodes),
            "edges": [],
        }
        if not nodes:
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
