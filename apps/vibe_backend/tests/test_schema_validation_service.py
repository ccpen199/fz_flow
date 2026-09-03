from app.services.schema_validation_service import build_schema_index, resolve_table_field


def test_schema_validation_resolves_table_and_field_without_case_sensitivity() -> None:
    schema_index = build_schema_index(
        {
            "tables": [
                {
                    "table_name": "dict_brand_info",
                    "table_comment": "brand dictionary",
                    "fields": [{"field_name": "Code"}, {"field_name": "Name"}],
                }
            ]
        }
    )

    resolved = resolve_table_field("DICT_BRAND_INFO", "name", schema_index=schema_index)

    assert resolved["ok"] is True
    assert resolved["table_name"] == "dict_brand_info"
    assert resolved["field_name"] == "Name"


def test_schema_validation_reports_missing_table_and_field() -> None:
    schema_index = build_schema_index(
        {
            "tables": [
                {
                    "table_name": "clothing_info",
                    "fields": [{"field_name": "BrandCode"}],
                }
            ]
        }
    )

    missing_table = resolve_table_field("missing_table", "Name", schema_index=schema_index)
    missing_field = resolve_table_field("clothing_info", "Name", schema_index=schema_index)

    assert missing_table["ok"] is False
    assert missing_table["reason"] == "table_not_found"
    assert missing_field["ok"] is False
    assert missing_field["reason"] == "field_not_found"
