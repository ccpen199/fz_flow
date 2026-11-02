"""轮次级配置解析：把「配置」从**会话属性**改成**按需加载的轮次资源**。

背景
----
配置（场景）原本是会话级的：会话创建时匹配一次，之后整场沿用（`conversation.scene_id`
一旦写入就不再重算）。这在真实对话里会出三种问题：

1. 用户被锁死——开场问了价格带，后面想改问纤维成分，用的还是价格带的配置；
2. 匹配本身是关键词打分且带默认兜底——实测「价格带分布」会被判到**竞品与价格分析**
   （"价格"命中竞品 +2、"价格带"命中价格带 +2，2:2 平手，遍历顺序里竞品靠前→先到者胜），
   完全不相关的输入则静默落到默认场景；
3. 不需要配置的轮次也在背配置——"换成折线图""把第 2 页挪到前面"根本用不到字段与关系。

现在改成：**每轮由多轮 agent 按问题判定需要哪些配置，后端按需加载**。
涉及配置就加载，不涉及就一个都不导入；同一会话的不同轮可以加载不同配置。

不做兜底
--------
配置由 agent 依据**配置目录**（真实存在于配置中心的那些）判定，白名单校验后加载。
判定不出来时返回空，由对话层决定是追问还是照常进行——**这里不猜、不做近义替换**。
会话级的关键词匹配（`conversation_service.resolve_scene`）保留给老链路使用，
新链路不再依赖它。

给下游的形状
------------
`load()` 返回的仍然是一个标准 `SceneDTO`（多配置时是合并出来的合成对象），
所以 `sql_result_agent_service` / `dimension_recommender` / `canvas_orchestrator`
**一行都不用改**。真实加载了哪些配置另记在返回的 info 里，进 lineage，保证可溯源。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from packages.shared_contracts.python_models import SceneDTO, SceneFieldDTO, SceneRelationDTO

from .scene_cache_service import scene_cache_service


# 目录里每个配置附带多少条样例目标。这些是配置中心里人工写好的原句，
# 原样交给 agent 做语义比对照，比后端再抽一层关键词更可靠（也不引入业务词表）。
CATALOG_SAMPLE_GOALS = 4


class TurnConfigService:
    """Resolves which configuration(s) a single turn actually needs."""

    # 「全部配置的并集」缓存：键是配置 id 组合，配置中心一改（增删配置）就失效。
    _all_signature: tuple[str, ...] | None = None
    _all_scene: SceneDTO | None = None

    # ---------------------------------------------------------------- catalog

    def all_configs_scene(self) -> SceneDTO | None:
        """全部配置的合并视图，**只用于"判定之前"的那一步**。

        为什么需要它：维度推荐发生在 agent 判定之前（agent 得先看到可选方向，才谈得上
        选方向），而此刻还不知道本轮会加载哪套配置。所以先用并集推荐一轮；等 agent 回了
        `config_keys`，再用**实际加载的配置**复核它选的方向与写在 filters 里的字段。
        这样既不存在"先有配置还是先有方向"的循环依赖，也不放权给任何一个配置。

        结果按配置 id 组合缓存：配置中心改了东西，id 组合通常会变，缓存自然失效。
        """
        scenes = self._scenes()
        signature = tuple(str(scene.scene_id) for scene in scenes)
        if self._all_signature == signature and self._all_scene is not None:
            return self._all_scene

        merged = self._merge(primary=None, scenes=scenes, name="全部分析配置", description="")
        self._all_signature = signature
        self._all_scene = merged
        return merged

    def catalog(self) -> list[dict[str, Any]]:
        """配置目录：交给 agent 用来判断「这一轮需要哪套配置」。

        刻意不含任何由代码硬编码的业务关键词——目录内容全部来自配置中心
        （配置自身的名称、描述、样例目标与字段清单）。因此新增一个配置后，
        它会自动出现在目录里，**不需要改代码**。
        """
        items: list[dict[str, Any]] = []
        for scene in self._scenes():
            fields = [field for field in scene.fields if getattr(field, "enabled", True)]
            if not fields:
                # 没有可用字段的配置无法支撑取数，不进目录（给了也只会误导）。
                continue
            items.append(
                {
                    "scene_id": str(scene.scene_id),
                    "name": str(scene.name or ""),
                    "description": str(scene.description or ""),
                    "sample_goals": [str(goal) for goal in (scene.sample_goals or [])[:CATALOG_SAMPLE_GOALS]],
                    "field_count": len(fields),
                    "fields": [
                        {
                            "semantic_name": str(getattr(field, "semantic_name", "") or ""),
                            "role": str(getattr(getattr(field, "role", ""), "value", "") or ""),
                        }
                        for field in fields
                    ],
                }
            )
        return items

    def field_lexicon(self) -> list[dict[str, Any]]:
        """全部配置字段的**并集**，每项标注它属于哪些配置。

        agent 写 `filters` 时需要一个「这个库里有哪些真实字段」的词表；但配置要等它
        判定完才知道是哪一套，于是形成循环。解法是先给它并集（实测去重后规模可控），
        等它回了 `config_keys` 之后，再用**本轮实际加载的配置**去校验它写下的字段：
        不在其中就如实说明并追问，而不是将就一个近似的列。
        """
        owners: dict[tuple[str, str, str], dict[str, Any]] = {}
        for scene in self._scenes():
            for field in scene.fields:
                if not getattr(field, "enabled", True):
                    continue
                semantic_name = str(getattr(field, "semantic_name", "") or "").strip()
                if not semantic_name:
                    continue
                key = (
                    str(getattr(field, "table_name", "") or ""),
                    str(getattr(field, "field_name", "") or ""),
                    semantic_name,
                )
                entry = owners.get(key)
                if entry is None:
                    entry = {
                        "semantic_name": semantic_name,
                        "role": str(getattr(getattr(field, "role", ""), "value", "") or ""),
                        "configs": [],
                    }
                    owners[key] = entry
                scene_id = str(scene.scene_id)
                if scene_id not in entry["configs"]:
                    entry["configs"].append(scene_id)
        return list(owners.values())

    # --------------------------------------------------------------- validate

    def validate_keys(self, keys: Any) -> tuple[list[str], list[str]]:
        """把 agent 给的 key 收敛到真实存在的配置上。

        返回 ``(有效 key, 被丢弃的 key)``。丢弃的一律**原样保留**给调用方去如实说明，
        不做大小写/近义/模糊匹配——那正是"猜"。
        """
        known = {str(scene.scene_id) for scene in self._scenes()}
        valid: list[str] = []
        rejected: list[str] = []
        for raw in keys if isinstance(keys, (list, tuple)) else []:
            key = str(raw or "").strip()
            if not key:
                continue
            if key in known:
                if key not in valid:
                    valid.append(key)
            elif key not in rejected:
                rejected.append(key)
        return valid, rejected

    # ------------------------------------------------------------------- load

    def load(self, keys: list[str]) -> tuple[SceneDTO | None, dict[str, Any]]:
        """按 key 加载配置。返回 ``(本轮配置, 说明)``。

        - 0 个 key → ``(None, ...)``：本轮**不加载任何配置**，这是正常结果，不是失败。
        - 1 个 key → 直接返回配置中心里那个真实对象。
        - 多个 key → 合并成一个合成配置：字段/关系取并集，``scene_id`` 取**首个真实 id**。

        为什么合并时 scene_id 用真实的那个而不是自造一个：``get_scene_playbook(scene_id)``
        是按 id 回查配置的，自造 id 查不到 playbook，会连带丢掉价格带策略与问题矩阵——
        那是实打实的能力损失。字段与关系则取并集，因为那才是"这一轮能用什么"。
        """
        scenes: list[SceneDTO] = []
        for key in keys:
            scene = scene_cache_service.get_scene(key)
            if scene is not None:
                scenes.append(scene)

        if not scenes:
            return None, {
                "config_ids": [],
                "names": [],
                "source": "turn",
                "merged": False,
                "note": "本轮没有加载配置。",
            }

        if len(scenes) == 1:
            scene = scenes[0]
            return scene, {
                "config_ids": [str(scene.scene_id)],
                "names": [str(scene.name or "")],
                "source": "turn",
                "merged": False,
                "note": "",
            }

        primary = scenes[0]
        names = [str(scene.name or scene.scene_id) for scene in scenes]
        merged = self._merge(
            primary=primary,
            scenes=scenes,
            name=" + ".join(name for name in names if name),
            description=str(primary.description or ""),
        )
        return merged, {
            "config_ids": [str(scene.scene_id) for scene in scenes],
            "names": names,
            "source": "turn",
            "merged": True,
            "note": f"本轮合并了 {len(scenes)} 套配置。",
        }

    # ---------------------------------------------------------------- reload

    def reload(self, config_ids: list[str]) -> SceneDTO | None:
        """按**已经判定过**的一份 ``config_ids`` 把当时那套配置再取回来。

        与 ``load`` 的区别只在语义，能力完全一致（合并口径也一致，所以合并多套时
        重放出来的字段并集与当轮取数用的完全相同）：

        - ``load`` 是"**决定**这一轮要加载什么"的唯一入口，因此要配合白名单校验
          与 ``plan_turn_config`` 的三条规则（"不涉及配置的轮次一个都不导入"等）。
        - 本方法是"**重放**"：画布上那三类动作（执行卡片自己的 SQL / 重跑某一页 /
          换一批维度）必须跟**本轮取数**用同一套配置，而那份配置早已判定完了并记在
          ``working_memory.turn_config.config_ids`` 里。这里不做校验、不做"涉及与否"
          的判断——那些在判定时已经做过，再做一次只会把已经决定的结果推翻。

        为什么需要它：配置是**轮次资源**，而 ``conversation.scene_id`` 只在建会话时
        由关键词猜一次、之后永不更新。画布动作若继续读会话级场景，就会出现
        "按 A 套取到的数，拿 B 套的字段去重算 / 去推荐方向"。
        """
        scene, _ = self.load([str(key) for key in (config_ids or []) if str(key or "").strip()])
        return scene

    # ---------------------------------------------------------------- helpers

    @classmethod
    def _merge(
        cls,
        *,
        primary: SceneDTO | None,
        scenes: list[SceneDTO],
        name: str,
        description: str,
    ) -> SceneDTO | None:
        """把多套配置合成一个标准 SceneDTO。

        ``scene_id`` 用 ``primary`` 的（真实 id，不是自造的），理由见 ``load`` 的说明；
        ``name`` / ``description`` 由调用方决定，因为「本轮加载了哪几套」和
        「全部配置的并集」在展示上是两回事。
        """
        if not scenes:
            return None
        anchor = primary or scenes[0]
        return SceneDTO(
            scene_id=str(anchor.scene_id),
            name=name,
            description=description,
            version=max(int(getattr(scene, "version", 1) or 1) for scene in scenes),
            sample_goals=cls._merge_sample_goals(scenes),
            fields=cls._merge_fields(scenes),
            relations=cls._merge_relations(scenes),
        )

    @staticmethod
    def _scenes() -> list[SceneDTO]:
        return scene_cache_service.list_scenes()

    @staticmethod
    def _merge_fields(scenes: list[SceneDTO]) -> list[SceneFieldDTO]:
        """字段并集，按 (表, 列) 去重。保留首次出现顺序，便于前端稳定展示。"""
        seen: dict[tuple[str, str], SceneFieldDTO] = {}
        for scene in scenes:
            for field in scene.fields:
                key = (
                    str(getattr(field, "table_name", "") or ""),
                    str(getattr(field, "field_name", "") or ""),
                )
                if key not in seen:
                    seen[key] = field
        return list(seen.values())

    @staticmethod
    def _merge_relations(scenes: list[SceneDTO]) -> list[SceneRelationDTO]:
        """关系并集，按两端 (表,列) 四元组去重。"""
        seen: dict[tuple[str, str, str, str], SceneRelationDTO] = {}
        for scene in scenes:
            for relation in scene.relations:
                key = (
                    str(getattr(relation, "left_table", "") or ""),
                    str(getattr(relation, "left_field", "") or ""),
                    str(getattr(relation, "right_table", "") or ""),
                    str(getattr(relation, "right_field", "") or ""),
                )
                if key not in seen:
                    seen[key] = relation
        return list(seen.values())

    @staticmethod
    def _merge_sample_goals(scenes: list[SceneDTO]) -> list[str]:
        goals: list[str] = []
        for scene in scenes:
            for goal in scene.sample_goals or []:
                text = str(goal or "").strip()
                if text and text not in goals:
                    goals.append(text)
        return goals

    @staticmethod
    def describe(config_info: dict[str, Any] | None) -> str:
        """一句话描述本轮加载的配置，供回复文案与前端头部使用。"""
        info = config_info or {}
        names = [str(name) for name in (info.get("names") or []) if str(name).strip()]
        if not names:
            return "未加载配置"
        return " + ".join(names)


turn_config_service = TurnConfigService()
