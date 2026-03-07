"""Tests for runtime.schema_validator — JSON Schema validation."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from runtime.schema_validator import (
    ActionSchemaValidator,
    SchemaValidationError,
    load_schemas,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WRITE_FILE_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}


@pytest.fixture
def validator() -> ActionSchemaValidator:
    return ActionSchemaValidator({"write_file": WRITE_FILE_SCHEMA})


# ---------------------------------------------------------------------------
# load_schemas tests
# ---------------------------------------------------------------------------

def test_load_schemas_from_directory() -> None:
    """Load valid schema files from a directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        schema_path = Path(tmpdir) / "greet.schema.json"
        schema_path.write_text(json.dumps({
            "type": "object",
            "properties": {"name": {"type": "string"}},
        }))
        registry = load_schemas(tmpdir)
        assert "greet" in registry


def test_load_schemas_skips_invalid() -> None:
    """Invalid JSON schema files are skipped."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bad = Path(tmpdir) / "bad.schema.json"
        bad.write_text("not json")
        registry = load_schemas(tmpdir)
        assert "bad" not in registry


def test_load_schemas_empty_dir() -> None:
    """Empty directory returns empty registry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        registry = load_schemas(tmpdir)
        assert registry == {}


def test_load_schemas_nonexistent_dir() -> None:
    """Nonexistent directory returns empty registry."""
    registry = load_schemas("/nonexistent/path")
    assert registry == {}


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------

def test_valid_params_pass(validator: ActionSchemaValidator) -> None:
    """Correct params pass validation."""
    validator.validate("write_file", {"path": "/tmp/x", "content": "hello"})


def test_missing_required_field_fails(validator: ActionSchemaValidator) -> None:
    """Missing required field raises SchemaValidationError."""
    with pytest.raises(SchemaValidationError) as exc_info:
        validator.validate("write_file", {"path": "/tmp/x"})
    assert "content" in str(exc_info.value)


def test_wrong_type_fails(validator: ActionSchemaValidator) -> None:
    """Wrong type raises SchemaValidationError."""
    with pytest.raises(SchemaValidationError):
        validator.validate("write_file", {"path": 123, "content": "hello"})


def test_additional_properties_fails(validator: ActionSchemaValidator) -> None:
    """Extra properties raise SchemaValidationError."""
    with pytest.raises(SchemaValidationError):
        validator.validate("write_file", {"path": "/x", "content": "y", "extra": True})


def test_empty_path_fails(validator: ActionSchemaValidator) -> None:
    """Empty string for minLength=1 field fails."""
    with pytest.raises(SchemaValidationError):
        validator.validate("write_file", {"path": "", "content": "y"})


def test_no_schema_allows_through(validator: ActionSchemaValidator) -> None:
    """Actions without a schema pass validation (open mode)."""
    # "read_file" has no schema in this validator
    validator.validate("read_file", {"anything": "goes"})


def test_registered_actions(validator: ActionSchemaValidator) -> None:
    """registered_actions returns list of schema'd actions."""
    assert validator.registered_actions == ["write_file"]


def test_multiple_errors_reported() -> None:
    """Multiple validation errors are all reported."""
    validator = ActionSchemaValidator({"write_file": WRITE_FILE_SCHEMA})
    with pytest.raises(SchemaValidationError) as exc_info:
        validator.validate("write_file", {})
    # Both path and content should be missing
    assert len(exc_info.value.errors) >= 2
