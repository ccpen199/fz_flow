"""空结果诊断：SQL 跑通但 0 行时，用**真实库**找出到底是哪个条件把结果筛空的。

为什么需要它（2026-10-17 真实报障，会话 `conv_dbc7be9bf4`）：

用户问「优衣库这个月上了多少件 T恤」，SQL 跑通但 0 行，画布弹出一张澄清卡，
给的却是两个**时间口径**选项。用户点了「仍限定在一个有数据的月份」，还是 0 行，
同一句提示再出现——因为真正筛空结果的是 `二级类目 = 'T恤'`：
该品牌这批数据里根本没有叫 `T恤` 的值（真实值是 `T恤/Polo衫/背心`，121 条）。
时间怎么换都没用，这是一个**必然的死循环**。

同一问题在老页面（`/api/v1/sql-result-agent/...`）复现结果完全一致：SQL 一样、也是 0 行。
所以根因不在某条链路，而在**标准值 vs 该品牌下的真实取值**——而旧实现让用户
自己去猜哪个条件是堵点，卡片却只给时间选项，等于没给出口。

本服务只做一件事：**逐条剔除筛选条件数一遍**，用真实计数指出是谁筛空的，
并列出该字段在剩余人群里的真实取值。找不到正向证据就如实返回 `checked=False`，
由调用方退回原有行为——**不猜**。

安全边界：
* 只读（COUNT / GROUP BY），不写任何表；
* 表名/列名只认 `scene` 字段白名单 + `scene.relations` 里声明过的连接，全部走参数化查询；
* 连接关系取自场景配置（`relations`），不自己编 JOIN；
* 主表有 `Id` 时用 `COUNT(DISTINCT 主表.Id)`，避免一对多关系把计数放大。
"""

from __future__ import annotations

import os
import re
from collections import deque
from typing import Any

import pymysql

from packages.shared_contracts.python_models import SceneDTO

# 备选值最多给几条：卡片上是要点选的，超过 4 条就不再是"一键处理"了。
MAX_ALTERNATIVES = 4

# 只支持这些比较运算符；认不出的条件直接跳过（不猜语义）。
_SUPPORTED_OPERATORS = {"=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "in": "IN", "like": "LIKE"}


def _mysql_config() -> dict[str, Any]:
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "root"),
        "database": os.getenv("MYSQL_DATABASE", "dataservice_test_local"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": True,
        "connect_timeout": 3,
        "read_timeout": 5,
    }


def _quote(identifier: str) -> str:
    return "`" + str(identifier).replace("`", "``") + "`"


def _connect():
    """单独一层连接入口：测试里可以 monkeypatch 掉，不必真的连库。"""
    return pymysql.connect(**_mysql_config())


class EmptyResultDiagnosisService:
    """找出把结果筛空的筛选条件，并给出该字段在剩余人群里的真实取值。"""

    def diagnose(
        self,
        *,
        scene: SceneDTO | None,
        filters: list[dict[str, Any]] | None,
        fixed_filters: list[dict[str, Any]] | None = None,
        limit: int = MAX_ALTERNATIVES,
        time_range: tuple[str, str] | None = None,
        time_field: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """找出把结果筛空的条件。

        `fixed_filters` 是**不能建议改动、但必须参与计数**的条件（典型：用户已确认的品牌）。
        它们不放进候选池（不该建议"去掉品牌"），但必须出现在每条 COUNT/GROUP BY 的 WHERE 里，
        否则备选值给的是**全表**行数——实测会提示"改用某值可查到 10814 条"，
        而该品牌这批数据的真实值只有 121 条，属于同一类误导。

        `time_range` 同理，而且更硬：这一轮问的是「最近一个月」，所有条数都必须是
        **这个月里**的条数。不带时间底座时，"去掉它可查到 378 条"会和卡片上
        "本月 13 条"直接打架（2026-10-19 实测）。
        """
        usable = self._usable_filters(scene, filters)
        fixed = self._usable_filters(scene, fixed_filters)
        for item in fixed:
            item["fixed"] = True
        usable = fixed + [item for item in usable if not self._same_field(item, fixed)]
        if not any(not item.get("fixed") for item in usable):
            return {"checked": False, "reason": "没有可诊断的结构化筛选条件"}

        # 以**堵点候选所在表**为主表（字典表通常是被 JOIN 的一侧，不做主表）。
        main_table = self._pick_main_table(usable, scene)
        tables = {str(item["table"]) for item in usable}

        edges = self._join_edges(scene, main_table, tables)
        if edges is None:
            return {"checked": False, "reason": "筛选条件涉及的表在场景关系里连不起来"}

        try:
            with _connect() as connection:
                with connection.cursor() as cursor:
                    builder = _QueryBuilder(
                        main_table=main_table,
                        edges=edges,
                        columns=self._scene_columns(scene),
                    )
                    builder.time_predicate = self._time_predicate(
                        builder=builder,
                        scene=scene,
                        time_range=time_range,
                        time_field=time_field,
                    )
                    return self._diagnose_with_cursor(
                        cursor=cursor, builder=builder, usable=usable, limit=limit
                    )
        except Exception as exc:  # noqa: BLE001
            # 连不上/超时/权限问题都只意味着"这次判不了"，绝不影响主链路。
            return {"checked": False, "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _time_predicate(
        *,
        builder: "_QueryBuilder",
        scene: SceneDTO | None,
        time_range: tuple[str, str] | None,
        time_field: tuple[str, str] | None,
    ) -> tuple[str, list[Any]] | None:
        """把这一轮的时间窗做成一条**始终生效**的 WHERE（所有计数都在窗口内）。

        拿不到合法日期、或时间列不在本次查询里 → 返回 None（宁可少一个底座，
        也不猜一个可能拼错的列名）。
        """
        if not time_range or not time_field:
            return None
        start, end = (str(part or "").strip() for part in time_range)
        if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end)):
            return None
        table, column = (str(part or "").strip() for part in time_field)
        alias = builder.aliases.get(table)
        if not alias:
            return None
        if column not in EmptyResultDiagnosisService._scene_columns(scene).get(table, set()):
            return None
        return (f"DATE({alias}.{_quote(column)}) BETWEEN %s AND %s", [start, end])

    @staticmethod
    def _scene_columns(scene: SceneDTO | None) -> dict[str, set[str]]:
        """表名 → 启用的列名集合。"""
        columns: dict[str, set[str]] = {}
        for field in (scene.fields if scene is not None else []):
            if not getattr(field, "enabled", True):
                continue
            table = str(getattr(field, "table_name", "") or "").strip()
            column = str(getattr(field, "field_name", "") or "").strip()
            if table and column:
                columns.setdefault(table, set()).add(column)
        return columns

    def _usable_filters(
        self, scene: SceneDTO | None, filters: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        """把筛选条件收敛成可安全下推的 (表, 列, 运算符, 值)；不合法的一律丢掉。"""
        allowed = self._scene_columns(scene)
        usable: list[dict[str, Any]] = []
        for item in filters or []:
            if not isinstance(item, dict):
                continue
            table = str(item.get("table_name") or "").strip()
            column = str(item.get("raw_field_name") or "").strip()
            if table not in allowed or column not in allowed[table]:
                continue
            operator = str(item.get("operator") or "=").strip().lower()
            if operator not in _SUPPORTED_OPERATORS:
                continue
            value = item.get("value")
            if isinstance(value, (list, tuple)):
                values = [str(entry).strip() for entry in value if str(entry).strip()]
                if not values:
                    continue
                value = values
            else:
                value = str(value or "").strip()
                if not value:
                    continue
            usable.append(
                {
                    "table": table,
                    "column": column,
                    "operator": operator,
                    "value": value,
                    "field": str(item.get("field") or column),
                    "display": str(item.get("display") or ""),
                }
            )
        return usable

    @staticmethod
    def _same_field(item: dict[str, Any], pool: list[dict[str, Any]]) -> bool:
        """同一个字段（表 + 列）是否已在 `pool` 里 —— 避免固定条件与筛选条件重复计数。"""
        return any(
            other["table"] == item["table"] and other["column"] == item["column"] for other in pool
        )

    @staticmethod
    def _pick_main_table(usable: list[dict[str, Any]], scene: SceneDTO | None) -> str:
        """优先用"非字典表"做主表：字典表（*_dict / dict_*）通常是被 JOIN 的一侧。"""
        for item in usable:
            table = str(item["table"])
            if not table.startswith("dict_"):
                return table
        return str(usable[0]["table"])

    @staticmethod
    def _join_edges(scene: SceneDTO | None, main_table: str, targets: set[str]):
        """从场景关系里找出连接 main_table 与各目标表所需的边（BFS，不自己编 JOIN）。"""
        if scene is None:
            return None
        adjacency: dict[str, list[tuple[str, Any]]] = {}
        for relation in scene.relations or []:
            left = str(getattr(relation, "left_table", "") or "")
            right = str(getattr(relation, "right_table", "") or "")
            if not left or not right:
                continue
            adjacency.setdefault(left, []).append((right, relation))
            adjacency.setdefault(right, []).append((left, relation))

        parent: dict[str, tuple[str, Any] | None] = {main_table: None}
        queue = deque([main_table])
        while queue:
            current = queue.popleft()
            for neighbour, relation in adjacency.get(current, []):
                if neighbour in parent:
                    continue
                parent[neighbour] = (current, relation)
                queue.append(neighbour)

        needed = {target for target in targets if target != main_table}
        for target in needed:
            if target not in parent:
                return None

        edges: list[Any] = []
        for target in needed:
            node = target
            while parent.get(node) is not None:
                previous, relation = parent[node]
                if relation not in edges:
                    edges.append(relation)
                node = previous
        return edges

    def _diagnose_with_cursor(self, *, cursor, builder: "_QueryBuilder", usable: list[dict[str, Any]], limit: int) -> dict[str, Any]:
        predicates = [builder.predicate(item) for item in usable]

        total_without_filters = builder.count(cursor)

        # 两个条件同时命中 0 行时，"剔掉任何一个都会出数"，所以"能剔出数"本身不能指认堵点。
        # 真正可指认的信号是：**该字段在这批数据里的真实取值，有没有"包含你说法的同族写法"**
        # （实测：`T恤` 在优衣库（官网）这批数据里写作 `T恤/Polo衫/背心`）。
        # 有同族写法的那个字段才是"标准值与该人群对不上"的堵点；另一个（通常是品牌/实体）
        # 是用户明确选的，不该被建议换掉。
        candidates: list[dict[str, Any]] = []
        for index, item in enumerate(usable):
            if item.get("fixed"):
                # 用户已确认的条件（品牌）只约束人群，不许被建议"去掉"或"换掉"。
                continue
            rest = [part for position, part in enumerate(predicates) if position != index]
            rows_if_dropped = builder.count(cursor, rest)
            if rows_if_dropped <= 0:
                continue
            values = builder.group_values(cursor, item["table"], item["column"], rest, limit=30)
            options = self._alternatives(item, values)
            candidates.append(
                {
                    "index": index,
                    "field": item["field"],
                    "column": item["column"],
                    "table": item["table"],
                    "value": item["value"],
                    "display": item["display"],
                    "rows_if_dropped": rows_if_dropped,
                    "alternatives": options["all"],
                    "siblings": options["siblings"],
                }
            )

        if not candidates:
            # 每个条件都剔过一遍，结果仍为 0 → 不是某个筛选条件的问题（可能是时间窗或数据本身），
            # 这时不敢断言，交回原有行为。
            return {
                "checked": True,
                "table": builder.main_table,
                "blockers": [],
                "alternatives": [],
                "rows_without_filters": total_without_filters,
            }

        primary = next((item for item in candidates if item["siblings"]), candidates[0])
        picked = primary["siblings"] or primary["alternatives"][:limit]
        return {
            "checked": True,
            "table": builder.main_table,
            "blockers": [
                {key: value for key, value in item.items() if key not in {"alternatives", "siblings"}}
                for item in candidates
            ],
            "blocker": {key: value for key, value in primary.items() if key not in {"alternatives", "siblings"}},
            "alternatives": picked,
            "rows_without_filters": total_without_filters,
        }

    @staticmethod
    def _alternatives(item: dict[str, Any], values: list[dict]) -> dict[str, list[dict[str, Any]]]:
        """把某字段在人群里的真实取值整理成备选；标出"包含原说法的同族写法"。"""
        current = str(item["value"]).strip()
        all_options: list[dict[str, Any]] = []
        siblings: list[dict[str, Any]] = []
        for row in values:
            raw = row.get("value")
            if raw is None:
                continue
            text = str(raw).strip()
            if not text or text == current:
                continue
            entry = {"field": item["field"], "value": text, "rows": int(row.get("n") or 0)}
            all_options.append(entry)
            if not (current and current.lower() in text.lower()):
                continue
            # ⚠️ 这里有两种完全不同的"包含"，卡片上的措辞必须分开：
            #   ① 合并标签：`T恤/Polo衫/背心` 是**把三个子类并成一个值**，用它回答
            #      「多少件 T恤」会把 Polo 衫和背心一起算进来 —— 必须如实标注"含…"，
            #      不能写成"改用某值"，那会让用户以为两者等价（2026-10-18 用户质疑）；
            #   ② 同族写法（拼写/别名差异）：语义相同，可以直接换。
            parts = [part.strip() for part in text.split("/") if part.strip()]
            if len(parts) > 1 and any(part.lower() == current.lower() for part in parts):
                entry["merged_parts"] = parts
            siblings.append(entry)
        return {"all": all_options, "siblings": siblings}



class _QueryBuilder:
    """用场景关系拼一条只读的 COUNT / GROUP BY 语句（表名列名全部来自白名单）。"""

    def __init__(self, *, main_table: str, edges: list[Any], columns: dict[str, set[str]]) -> None:
        self.main_table = main_table
        self.columns = columns
        self.aliases: dict[str, str] = {main_table: "t0"}
        self.joins: list[str] = []
        # 边的顺序来自 BFS 回溯，**不保证**靠近主表的先出现（`main→A→B` 会先拿到 A-B）。
        # 早期实现按原序直接 JOIN，第一条边两端都还没注册 → KeyError → 整个诊断静默降级成
        # "判不了"。这里改成反复扫：只添加"至少一端已注册"的边，直到没有进展为止。
        pending = list(edges)
        while pending:
            progressed = False
            for relation in list(pending):
                if self._add_join(relation):
                    pending.remove(relation)
                    progressed = True
            if not progressed:
                raise ValueError(
                    f"场景关系无法从 {main_table} 连通到全部筛选表："
                    f"{[getattr(item, 'relation_name', '?') for item in pending]}"
                )
        # 时间底座：本轮的绝对窗口，所有 COUNT/GROUP BY 都必须带上它。
        self.time_predicate: tuple[str, list[Any]] | None = None
        self.count_expr = self._count_expression()

    def _add_join(self, relation) -> bool:
        left = str(getattr(relation, "left_table", "") or "")
        right = str(getattr(relation, "right_table", "") or "")
        left_field = str(getattr(relation, "left_field", "") or "")
        right_field = str(getattr(relation, "right_field", "") or "")
        if left in self.aliases and right in self.aliases:
            return True  # 环路/已连通，无需再 JOIN
        if left not in self.aliases and right not in self.aliases:
            return False  # 这条边现在还接不上，留给下一轮
        known, unknown = (left, right) if left in self.aliases else (right, left)
        alias = f"t{len(self.aliases)}"
        self.aliases[unknown] = alias
        known_alias = self.aliases[known]
        if known == left:
            on = f"{alias}.{_quote(right_field)} = {known_alias}.{_quote(left_field)}"
        else:
            on = f"{alias}.{_quote(left_field)} = {known_alias}.{_quote(right_field)}"
        join_type = str(getattr(relation, "join_type", "LEFT") or "LEFT").upper()
        if join_type not in {"LEFT", "INNER", "RIGHT"}:
            join_type = "LEFT"
        self.joins.append(f"{join_type} JOIN {_quote(unknown)} {alias} ON {on}")
        return True

    def _count_expression(self) -> str:
        """主表有 Id 就用 COUNT(DISTINCT 主表.Id)：一对多的 JOIN 不会把计数放大。"""
        alias = self.aliases[self.main_table]
        key = next((name for name in self.columns.get(self.main_table, set()) if name.lower() == "id"), None)
        return f"COUNT(DISTINCT {alias}.{_quote(key)})" if key else "COUNT(*)"

    def from_sql(self) -> str:
        sql = f"FROM {_quote(self.main_table)} {self.aliases[self.main_table]}"
        if self.joins:
            sql += " " + " ".join(self.joins)
        return sql

    def predicate(self, item: dict[str, Any]) -> tuple[str, list[Any]]:
        alias = self.aliases[str(item["table"])]
        column = f"{alias}.{_quote(str(item['column']))}"
        operator = _SUPPORTED_OPERATORS[item["operator"]]
        value = item["value"]
        if isinstance(value, list):
            placeholders = ", ".join(["%s"] * len(value))
            return f"{column} IN ({placeholders})", list(value)
        if item["operator"] == "like":
            pattern = value if "%" in value else f"%{value}%"
            return f"{column} LIKE %s", [pattern]
        return f"{column} {operator} %s", [value]

    def count(self, cursor, predicates: list[tuple[str, list[Any]]] | None = None) -> int:
        sql = f"SELECT {self.count_expr} AS n {self.from_sql()}"
        conditions, params = self._where(predicates)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        cursor.execute(sql, params)
        row = cursor.fetchone() or {}
        return int(row.get("n") or 0)

    def group_values(self, cursor, table: str, column: str, predicates: list[tuple[str, list[Any]]], limit: int) -> list[dict]:
        alias = self.aliases[str(table)]
        target = f"{alias}.{_quote(str(column))}"
        sql = f"SELECT {target} AS value, {self.count_expr} AS n {self.from_sql()}"
        conditions, params = self._where(predicates)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += f" GROUP BY {target} ORDER BY n DESC LIMIT {int(limit)}"
        cursor.execute(sql, params)
        return list(cursor.fetchall() or [])

    def _where(
        self, predicates: list[tuple[str, list[Any]]] | None
    ) -> tuple[list[str], list[Any]]:
        """时间底座永远在最前：所有计数都限定在这一轮的窗口里。"""
        parts: list[tuple[str, list[Any]]] = []
        if self.time_predicate:
            parts.append(self.time_predicate)
        parts.extend(predicates or [])
        conditions: list[str] = []
        params: list[Any] = []
        for condition, values in parts:
            conditions.append(condition)
            params.extend(values)
        return conditions, params


empty_result_diagnosis_service = EmptyResultDiagnosisService()
