"""BlueprintStore — manages role and team blueprints for cluster provisioning.

Blueprints live in a directory tree:
    blueprints/
    ├── INDEX.yaml          # catalog of all blueprints
    ├── roles/
    │   ├── pm.md
    │   ├── analyst.md
    │   ├── architect.md
    │   ├── developer.md
    │   ├── tester.md
    │   └── reviewer.md
    ├── teams/
    │   ├── standard-cluster.yaml
    │   └── minimal-cluster.yaml
    └── generated/          # LLM-generated blueprints saved for reuse

Phase 5: v6.0 Cluster Provisioning.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Supported blueprint types
BLUEPRINT_TYPES = {"role", "team", "generated"}


class BlueprintNotFoundError(Exception):
    """Raised when a requested blueprint doesn't exist."""


class BlueprintStore:
    """Manages role and team blueprints on disk.

    Blueprints are stored in a directory tree. INDEX.yaml is the catalog.
    Files are loaded lazily — only read from disk on demand.
    """

    def __init__(self, blueprints_dir: str | Path) -> None:
        self._base = Path(blueprints_dir)
        self._index: list[dict[str, Any]] = []
        self._index_loaded = False

    async def startup_load(self) -> int:
        """Load and validate the blueprint catalog from INDEX.yaml.

        Returns number of blueprints found.
        """
        index_path = self._base / "INDEX.yaml"
        if not index_path.exists():
            logger.warning("Blueprint INDEX.yaml not found at %s — no blueprints available", index_path)
            return 0

        def _read() -> list[dict]:
            with open(index_path, "r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
            return raw.get("blueprints", [])

        try:
            entries = await asyncio.to_thread(_read)
            self._index = entries
            self._index_loaded = True
            logger.info("BlueprintStore loaded %d blueprint(s) from %s", len(entries), index_path)
            return len(entries)
        except Exception as exc:
            logger.error("Failed to load blueprint INDEX.yaml: %s", exc)
            return 0

    async def list_blueprints(
        self,
        blueprint_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return blueprint metadata from the index.

        Optionally filter by type ("role" | "team" | "generated") or tags.
        """
        if not self._index_loaded:
            await self.startup_load()

        result = list(self._index)

        if blueprint_type:
            result = [b for b in result if b.get("type") == blueprint_type]

        if tags:
            tag_set = set(tags)
            result = [b for b in result if tag_set.intersection(set(b.get("tags", [])))]

        return result

    async def get_blueprint(self, blueprint_type: str, blueprint_id: str) -> str:
        """Load and return the raw content of a blueprint.

        Args:
            blueprint_type: "role" | "team" | "generated"
            blueprint_id:   e.g. "pm", "standard-cluster", "my-custom-blueprint"

        Returns:
            Raw file content (Markdown for roles, YAML for teams).

        Raises:
            BlueprintNotFoundError if the file doesn't exist.
        """
        if blueprint_type not in BLUEPRINT_TYPES:
            raise BlueprintNotFoundError(
                f"Unknown blueprint type '{blueprint_type}'. Must be one of: {BLUEPRINT_TYPES}"
            )

        # Infer file extension from type
        ext = ".md" if blueprint_type == "role" else ".yaml"

        # The correct subdir naming:
        subdir_map = {
            "role": "roles",
            "team": "teams",
            "generated": "generated",
        }
        subdir = subdir_map[blueprint_type]
        candidates = [
            self._base / subdir / f"{blueprint_id}{ext}",
            self._base / subdir / blueprint_id,
        ]

        for path in candidates:
            if path.exists():
                def _read(p: Path) -> str:
                    return p.read_text(encoding="utf-8")
                return await asyncio.to_thread(_read, path)

        raise BlueprintNotFoundError(
            f"Blueprint not found: type={blueprint_type}, id={blueprint_id} "
            f"(searched: {[str(c) for c in candidates]})"
        )

    async def get_role_blueprint(self, role: str) -> str:
        """Convenience: load a role blueprint by role name."""
        return await self.get_blueprint("role", role)

    async def get_team_blueprint(self, team_id: str) -> dict[str, Any]:
        """Convenience: load and parse a team YAML blueprint."""
        content = await self.get_blueprint("team", team_id)
        return yaml.safe_load(content) or {}

    async def save_blueprint(
        self,
        blueprint_type: str,
        blueprint_id: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Save a generated blueprint to disk and update the index.

        Returns the saved file path.
        """
        subdir_map = {
            "role": "roles",
            "team": "teams",
            "generated": "generated",
        }
        subdir = subdir_map.get(blueprint_type, "generated")
        ext = ".md" if blueprint_type == "role" else ".yaml"
        target_dir = self._base / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{blueprint_id}{ext}"

        def _write() -> None:
            file_path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
        logger.info("Saved blueprint: %s/%s → %s", blueprint_type, blueprint_id, file_path)

        # Update in-memory index
        new_entry: dict[str, Any] = {
            "id": blueprint_id,
            "type": blueprint_type,
            "name": (meta or {}).get("name", blueprint_id),
            "description": (meta or {}).get("description", ""),
            "tags": (meta or {}).get("tags", []),
            "path": f"{subdir}/{blueprint_id}{ext}",
        }
        # Replace existing entry or append
        existing = [i for i, b in enumerate(self._index) if b.get("id") == blueprint_id and b.get("type") == blueprint_type]
        if existing:
            self._index[existing[0]] = new_entry
        else:
            self._index.append(new_entry)

        # Persist updated INDEX.yaml
        await self._flush_index()
        return str(file_path)

    async def _flush_index(self) -> None:
        """Write the in-memory index back to INDEX.yaml."""
        index_path = self._base / "INDEX.yaml"
        index_path.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            with open(index_path, "w", encoding="utf-8") as fh:
                yaml.dump(
                    {"blueprints": self._index},
                    fh,
                    default_flow_style=False,
                    allow_unicode=True,
                )

        await asyncio.to_thread(_write)
        logger.debug("Blueprint INDEX.yaml flushed (%d entries)", len(self._index))

    def blueprint_dir(self) -> str:
        """Return the base directory path for blueprints."""
        return str(self._base)
