from __future__ import annotations

from typing import Any

from .prd_scene_configs import get_prd_scene_config


def get_scene_playbook(scene_id: str, scene_name: str | None = None) -> dict[str, Any] | None:
    config = get_prd_scene_config(scene_id=scene_id, scene_name=scene_name)
    if not config:
        return None
    return _prd_scene_playbook(scene_id=scene_id, scene_name=scene_name or config["name"], config=config)


def _prd_scene_playbook(scene_id: str, scene_name: str, config: dict[str, Any]) -> dict[str, Any]:
    question_matrix = config.get("question_matrix") or []
    return {
        "scene_id": scene_id,
        "scene_name": scene_name,
        "panel_version": "v2",
        "scope": config.get("scope") or "prd-scene-question-config",
        "fields": config.get("fields") or [],
        "relations": config.get("relations") or [],
        "metric_templates": config.get("metric_templates") or [],
        "price_band_template": config.get("price_band_template") or [],
        "question_matrix": question_matrix,
        "regression_questions": [item["question"] for item in question_matrix],
        "out_of_scope": config.get("out_of_scope") or [],
        "notes": [
            "preset_key 是稳定内部标识，question 文案可以编辑但不要改掉 key",
            *list(config.get("notes") or []),
        ],
    }
