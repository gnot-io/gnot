"""Action parameter schema validation.

Loads JSON Schema files co-located with action modules and validates
incoming params before execution. Schema files follow the naming
convention ``{action_name}.schema.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema import Draft7Validator, ValidationError

from runtime.models import ActionSpec, CallerCredentialSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

SchemaRegistry = dict[str, dict[str, Any]]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SchemaValidationError(Exception):
    """Raised when action params fail schema validation."""

    def __init__(self, action_name: str, errors: list[str]) -> None:
        self.action_name = action_name
        self.errors = errors
        detail = "; ".join(errors)
        super().__init__(f"Schema validation failed for '{action_name}': {detail}")


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_schemas(actions_dir: str | Path) -> SchemaRegistry:
    """Load all JSON Schema files from an actions directory.

    Expects files named ``{action_name}.schema.json`` alongside the
    corresponding ``.py`` action modules.

    Args:
        actions_dir: Path to the actions directory.

    Returns:
        A dict mapping action names to their parsed JSON Schema.
    """
    directory = Path(actions_dir)
    registry: SchemaRegistry = {}

    if not directory.is_dir():
        return registry

    for schema_file in sorted(directory.glob("*.schema.json")):
        action_name = schema_file.name.replace(".schema.json", "")
        try:
            with open(schema_file, "r", encoding="utf-8") as fh:
                schema = json.load(fh)
            # Pre-validate the schema itself
            Draft7Validator.check_schema(schema)
            registry[action_name] = schema
            logger.info("Loaded schema for action: %s", action_name)
        except (json.JSONDecodeError, jsonschema.SchemaError) as exc:
            logger.error("Invalid schema file %s: %s", schema_file, exc)
            continue

    logger.info("Schema registry ready — %d schema(s) loaded", len(registry))
    return registry


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class ActionSchemaValidator:
    """Validates action parameters against registered JSON Schemas.

    Actions without a registered schema are allowed through (open validation).
    """

    def __init__(self, schema_registry: SchemaRegistry) -> None:
        self._schemas = schema_registry
        self._validators: dict[str, Draft7Validator] = {}
        for name, schema in schema_registry.items():
            self._validators[name] = Draft7Validator(schema)

    def validate(self, action_name: str, params: dict[str, Any]) -> None:
        """Validate params against the action's schema.

        Args:
            action_name: The action whose schema to check.
            params: The incoming parameters dict.

        Raises:
            SchemaValidationError: If validation fails.
        """
        validator = self._validators.get(action_name)
        if validator is None:
            # No schema registered → allow through
            logger.debug("No schema for action '%s' — skipping validation", action_name)
            return

        errors: list[str] = []
        for error in validator.iter_errors(params):
            path = " → ".join(str(p) for p in error.absolute_path) if error.absolute_path else "(root)"
            errors.append(f"[{path}] {error.message}")

        if errors:
            logger.warning(
                "Schema validation failed for %s: %d error(s)",
                action_name,
                len(errors),
            )
            raise SchemaValidationError(action_name, errors)

        logger.debug("Schema validation passed for %s", action_name)

    @property
    def registered_actions(self) -> list[str]:
        """Return the list of actions that have schemas."""
        return list(self._schemas.keys())

    def get_caller_credential_requirements(
        self, action_name: str
    ) -> dict[str, CallerCredentialSpec]:
        """Return the x-caller-credentials dict for an action.

        Returns an empty dict if the action has no schema or no
        x-caller-credentials section.
        """
        schema = self._schemas.get(action_name, {})
        raw = schema.get("x-caller-credentials", {})
        result: dict[str, CallerCredentialSpec] = {}
        for key, spec in raw.items():
            result[key] = CallerCredentialSpec(
                description=spec.get("description", ""),
                required=spec.get("required", True),
                hint=spec.get("hint", ""),
            )
        return result

    def check_caller_credentials(
        self,
        action_name: str,
        caller_credentials: dict[str, str],
    ) -> list[str]:
        """Return list of missing required credential keys for action_name.

        Empty list means all requirements are satisfied.
        """
        reqs = self.get_caller_credential_requirements(action_name)
        missing = [
            key for key, spec in reqs.items()
            if spec.required and key not in caller_credentials
        ]
        return missing

    def build_action_spec(self, action_name: str, module: object | None = None) -> ActionSpec:
        """Build an ActionSpec for one action from its schema + module metadata."""
        schema = self._schemas.get(action_name, {})
        is_async = getattr(module, "ASYNC", False) if module else False
        return ActionSpec(
            description=schema.get("description", ""),
            params_schema=schema.get("properties", {}),
            caller_credentials=self.get_caller_credential_requirements(action_name),
            async_action=bool(is_async),
        )

    def build_all_action_specs(
        self, registry: dict | None = None
    ) -> dict[str, ActionSpec]:
        """Build ActionSpec for every action that has a schema.

        registry: optional action module registry to detect ASYNC flag.
        """
        reg = registry or {}
        return {
            name: self.build_action_spec(name, reg.get(name))
            for name in self._schemas
        }
