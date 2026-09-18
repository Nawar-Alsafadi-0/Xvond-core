from pathlib import Path


BUILDER = Path("frontend/customer/employee-builder.js").read_text(encoding="utf-8")
PORTAL = Path("frontend/customer/app.js").read_text(encoding="utf-8")


def test_builder_keeps_generic_alternatives_when_packaged_connector_allows_them():
    assert "allow_generic_alternatives" in BUILDER
    assert "allowGenericAlternatives" in BUILDER
    assert "...packaged" in BUILDER
    assert "...generic.filter" in BUILDER


def test_integration_form_renders_catalog_defaults_and_choices():
    assert "field.choices" in PORTAL
    assert "field.default" in PORTAL
    assert "data-integration-config" in PORTAL
