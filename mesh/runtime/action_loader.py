"""Action plugin loader.

Scans a directory of Python modules and builds an action registry.
Each module must expose a ``run(params, context)`` function.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

ActionRegistry = dict[str, ModuleType]

# Required callable in every action module
REQUIRED_CALLABLE: str = "run"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_actions(actions_dir: str | Path) -> ActionRegistry:
    """Scan a directory and import all valid action modules.

    A valid action module is a ``.py`` file that exposes a callable
    named ``run(params: dict, context: dict) -> dict``.

    Args:
        actions_dir: Path to the actions directory.

    Returns:
        A dict mapping action names (filename without extension)
        to imported module objects.
    """
    directory = Path(actions_dir)
    registry: ActionRegistry = {}

    if not directory.is_dir():
        logger.warning("Actions directory does not exist: %s", directory)
        return registry

    for py_file in sorted(directory.glob("*.py")):
        if py_file.name.startswith("_"):
            continue

        action_name = py_file.stem
        try:
            module = _import_module(action_name, py_file)
        except Exception:
            logger.exception("Failed to import action module: %s", py_file)
            continue

        if not hasattr(module, REQUIRED_CALLABLE) or not callable(getattr(module, REQUIRED_CALLABLE)):
            logger.warning(
                "Action module %s missing callable '%s' — skipped",
                py_file.name,
                REQUIRED_CALLABLE,
            )
            continue

        registry[action_name] = module
        is_async = getattr(module, "ASYNC", False)
        logger.info(
            "Loaded action: %s (async=%s) from %s",
            action_name,
            is_async,
            py_file,
        )

    logger.info("Action registry ready — %d action(s) loaded", len(registry))
    return registry


def _import_module(name: str, path: Path) -> ModuleType:
    """Dynamically import a Python file as a module.

    Args:
        name: Module name to assign.
        path: Filesystem path to the .py file.

    Returns:
        The imported module.
    """
    spec = importlib.util.spec_from_file_location(f"actions.{name}", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module
