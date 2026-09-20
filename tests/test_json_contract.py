import pytest

from backend.app.modules.integrations.json_contract import (
    sanitize_json_contract,
    shape_json_value,
)


def test_nested_json_contract_shapes_declared_fields():
    contract = sanitize_json_contract(
        {
            "type": "object",
            "required": ["customer"],
            "properties": {
                "customer": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "profile": {
                            "type": "object",
                            "properties": {
                                "age": {"type": "integer"},
                            },
                        },
                    },
                },
                "items": {
                    "type": "array",
                    "max_items": 2,
                    "items": {
                        "type": "object",
                        "required": ["sku"],
                        "properties": {
                            "sku": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                    },
                },
            },
        }
    )

    shaped = shape_json_value(
        {
            "customer": {
                "name": "Nawar",
                "ignored": "drop",
                "profile": {"age": 31, "ignored": True},
            },
            "items": [
                {"sku": "A", "quantity": 2, "ignored": "drop"},
                {"sku": "B", "quantity": 1},
            ],
            "ignored_root": "drop",
        },
        contract,
    )

    assert shaped == {
        "customer": {
            "name": "Nawar",
            "profile": {"age": 31},
        },
        "items": [
            {"sku": "A", "quantity": 2},
            {"sku": "B", "quantity": 1},
        ],
    }


def test_nested_json_contract_rejects_missing_wrong_type_and_oversize_array():
    contract = sanitize_json_contract(
        {
            "type": "object",
            "properties": {
                "customer": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                "items": {
                    "type": "array",
                    "max_items": 1,
                    "items": {"type": "string"},
                },
            },
        }
    )

    with pytest.raises(ValueError, match=r"customer.*missing required"):
        shape_json_value({"customer": {}}, contract)

    with pytest.raises(ValueError, match=r"customer\.name must be a string"):
        shape_json_value({"customer": {"name": 123}}, contract)

    with pytest.raises(ValueError, match=r"items accepts at most 1 items"):
        shape_json_value({"items": ["a", "b"]}, contract)


def test_free_form_object_is_still_bounded():
    contract = sanitize_json_contract({"type": "object"})
    shaped = shape_json_value({"a": {"b": [1, 2, 3]}}, contract)
    assert shaped == {"a": {"b": [1, 2, 3]}}

    with pytest.raises(ValueError, match=r"accepts at most 50 fields"):
        shape_json_value(
            {f"k{index}": index for index in range(51)},
            contract,
        )
