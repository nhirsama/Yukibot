from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path("src/yukibot")


def imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
    return tuple(modules)


def test_features_do_not_import_other_features() -> None:
    violations: list[str] = []
    features_root = SOURCE_ROOT / "features"
    for feature_dir in (path for path in features_root.iterdir() if path.is_dir()):
        for source in feature_dir.rglob("*.py"):
            for module in imported_modules(source):
                prefix = "yukibot.features."
                if module.startswith(prefix):
                    imported_feature = module.removeprefix(prefix).split(".", maxsplit=1)[0]
                    if imported_feature != feature_dir.name:
                        violations.append(f"{source}: {module}")
    assert violations == []


def test_contracts_and_kernel_do_not_depend_on_outer_layers() -> None:
    forbidden = (
        "yukibot.adapters",
        "yukibot.bootstrap",
        "yukibot.config",
        "yukibot.features",
    )
    violations: list[str] = []
    for layer in ("contracts", "kernel"):
        for source in (SOURCE_ROOT / layer).rglob("*.py"):
            for module in imported_modules(source):
                if module.startswith(forbidden):
                    violations.append(f"{source}: {module}")
    assert violations == []


def test_generic_adapters_do_not_depend_on_features() -> None:
    violations: list[str] = []
    for source in (SOURCE_ROOT / "adapters").rglob("*.py"):
        for module in imported_modules(source):
            if module.startswith("yukibot.features"):
                violations.append(f"{source}: {module}")
    assert violations == []


def test_external_sdks_only_exist_in_adapters() -> None:
    violations: list[str] = []
    for source in SOURCE_ROOT.rglob("*.py"):
        if "adapters" in source.parts:
            continue
        for module in imported_modules(source):
            if module.split(".", maxsplit=1)[0] in {"aiosqlite", "telethon"}:
                violations.append(f"{source}: {module}")
    assert violations == []


def test_forwarder_core_does_not_depend_on_framework() -> None:
    integration_files = {"feature.py", "migrations.py", "repository.py"}
    violations: list[str] = []
    root = SOURCE_ROOT / "features" / "forwarder"
    for source in root.glob("*.py"):
        if source.name in integration_files or source.name == "__init__.py":
            continue
        for module in imported_modules(source):
            if module.startswith(("yukibot.adapters", "yukibot.kernel")):
                violations.append(f"{source}: {module}")
    assert violations == []


def test_forwarder_public_api_does_not_eagerly_load_integration_layers() -> None:
    source = SOURCE_ROOT / "features" / "forwarder" / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    integration_modules = {
        "feature",
        "infrastructure",
        "job_repository",
        "migrations",
        "repository",
        "worker",
    }
    imported = {
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > 0 and node.module
    }
    assert imported.isdisjoint(integration_modules)
