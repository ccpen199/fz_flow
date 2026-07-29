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
    price_band_template = config.get("price_band_template") or []
    price_band_policy = dict(config.get("price_band_policy") or {})
    default_mode = str(price_band_policy.get("default_mode") or "adaptive").strip().lower()
    if default_mode != "adaptive":
        default_mode = "adaptive"
    try:
        adaptive_bucket_count = int(price_band_policy.get("adaptive_bucket_count") or 5)
    except (TypeError, ValueError):
        adaptive_bucket_count = 5
    adaptive_bucket_count = max(2, min(adaptive_bucket_count, 20))
    strategy = str(price_band_policy.get("strategy") or "equal_width").strip().lower() or "equal_width"
    if strategy == "rounded_width":
        strategy = "equal_width"
    if strategy not in {"quantile", "equal_width"}:
        strategy = "equal_width"
    boundary = price_band_policy.get("boundary") if isinstance(price_band_policy.get("boundary"), dict) else {}
    return {
        "scene_id": scene_id,
        "scene_name": scene_name,
        "panel_version": "v2",
        "scope": config.get("scope") or "prd-scene-question-config",
        "fields": config.get("fields") or [],
        "relations": config.get("relations") or [],
        "metric_templates": config.get("metric_templates") or [],
        "price_band_template": price_band_template,
        "price_band_policy": {
            "default_mode": default_mode,
            "adaptive_bucket_count": adaptive_bucket_count,
            "strategy": strategy,
            "boundary": {
                "enabled": bool(boundary.get("enabled", False)),
                "rounding": str(boundary.get("rounding") or "auto").strip().lower() or "auto",
                "open_ended": bool(boundary.get("open_ended", True)),
                "custom_boundaries": boundary.get("custom_boundaries") if isinstance(boundary.get("custom_boundaries"), list) else [],
            },
            "mode_options": ["adaptive"],
        },
        "question_matrix": question_matrix,
        "regression_questions": [item["question"] for item in question_matrix],
        "out_of_scope": config.get("out_of_scope") or [],
        "notes": [
            "preset_key 是稳定内部标识，question 文案可以编辑但不要改掉 key",
            *list(config.get("notes") or []),
        ],
    }
