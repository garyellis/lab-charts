"""Executable form of the layer diagram.

Three invariants keep `cli/` a thin surface, so the same capabilities can be
fronted by a REST API, a GraphQL resolver, an RPC handler, or a Slack Bolt
app without re-implementing the CLI:

  (a) `cli/` may import `services/`, `plumbing/`, and the composition root --
      never `integrations/` directly. Enforced by ruff's flake8-tidy-imports
      banned-api (TID251), configured in pyproject.toml; run it with
      `uv run ruff check --select TID251 src/`. It is a lint rule rather than
      a test here because ruff already parses every file and per-file-ignores
      express "which directories may reach for an adapter" directly.

  (b) No module under `services/` may drag Rich or Typer into the process.
      `test_no_service_module_imports_rich_or_typer` below.

  (c) `sys.exit` belongs to the surface layer. A service that kills the
      process cannot be called by a server that must return a response.
      `test_no_process_exit_outside_cli` below.

Why (c) is a test and not a second banned-api entry: ruff reports every
banned-api violation under the single code TID251, and per-file-ignores are
per-code. Rule (a) needs TID251 *lifted* inside `services/` (services are
supposed to wrap adapters), while rule (c) needs it *enforced* there -- that
is exactly where a stray `sys.exit` would do the most damage. One code cannot
be both, so (c) is checked here instead of weakening (a).

A fourth invariant -- versioned wire contracts live in `services/*/wire.py`,
never in `cli/` -- is enforced separately in `tests/test_wire_contracts.py`,
because it is about dict literals rather than imports and so is invisible to
both TID251 and the scans here.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_PKG = _SRC / "chart_manager"
_SERVICES = _PKG / "services"
_PLUMBING = _PKG / "plumbing"
_INTEGRATIONS = _PKG / "integrations"

#: Only the surface layer may terminate the process.
_EXIT_ALLOWED_DIRS = (_PKG / "cli",)

#: Import either of these and a headless surface has a TUI in its address space.
_FORBIDDEN_ROOTS = ("rich", "typer")

# Chart concepts are service-domain policy, not generic plumbing.
_DOMAIN_MODULES_FORBIDDEN_IN_PLUMBING = {
    "chart_deps.py",
    "charts.py",
    "cluster_tests.py",
    "graph.py",
    "install_plan.py",
    "spec.py",
}


def test_chart_domain_modules_stay_out_of_plumbing() -> None:
    """Keep chart models and policy in services/domain, not generic utilities."""
    misplaced = sorted(
        path.name
        for path in _PLUMBING.glob("*.py")
        if path.name in _DOMAIN_MODULES_FORBIDDEN_IN_PLUMBING
    )
    assert not misplaced, (
        "chart-domain modules belong in chart_manager/services/domain, "
        f"not plumbing: {', '.join(misplaced)}"
    )

    expected = {
        "chart_deps.py",
        "charts.py",
        "install_plan.py",
    }
    actual = {
        path.name for path in (_SERVICES / "domain").glob("*.py") if path.name != "__init__.py"
    }
    assert expected <= actual


def test_validation_domain_modules_stay_out_of_plumbing() -> None:
    """Keep validation models and schema parsing with the validation service."""
    misplaced = sorted(
        path.name
        for path in (
            _PLUMBING / "validate_models.py",
            _PLUMBING / "validate_spec.py",
        )
        if path.exists()
    )
    assert not misplaced, (
        "validation-domain modules belong in "
        "chart_manager/services/manifest_validation, not plumbing: "
        f"{', '.join(misplaced)}"
    )

    validation_domain = _SERVICES / "manifest_validation"
    expected = {"models.py", "output_paths.py"}
    actual = {path.name for path in validation_domain.glob("*.py") if path.name != "__init__.py"}
    assert expected <= actual


def test_plumbing_does_not_import_service_domains() -> None:
    """Generic plumbing may not depend on chart or validation service policy."""
    forbidden_prefixes = (
        "chart_manager.services.domain",
        "chart_manager.services.manifest_validation",
    )
    offenders: list[str] = []

    for path in sorted(_PLUMBING.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = (node.module,)
            for module in imported:
                if module.startswith(forbidden_prefixes):
                    rel = path.relative_to(_SRC)
                    offenders.append(f"{rel}:{node.lineno}: {module}")

    assert not offenders, (
        "generic plumbing must not import service-domain modules:\n  " + "\n  ".join(offenders)
    )


def test_integration_modules_are_discoverable() -> None:
    """Guard the guard: an empty sweep would make the next test vacuously pass."""
    paths = sorted(_INTEGRATIONS.rglob("*.py"))
    assert len(paths) > 8, f"suspiciously few integration modules found: {paths}"
    assert _INTEGRATIONS / "helm.py" in paths
    assert _INTEGRATIONS / "kubectl.py" in paths


def test_integrations_do_not_import_services() -> None:
    """Adapters are the bottom of the stack: services depend on them, not back.

    TID251 cannot catch this direction. The banned-api entry names
    `chart_manager.integrations` -- the module that may not be *imported* --
    and is lifted inside `integrations/` itself, so an adapter reaching up
    into `services/` is invisible to it.

    Whatever an adapter needs from a service is passed in by the caller:
    `Helm` takes its dependency-freshness predicates as constructor
    arguments, wired from `services/domain/chart_deps` in
    `chart_manager.composition`, so the helm wrapper never has to know how a
    Chart.lock is parsed.
    """
    forbidden_prefixes = ("chart_manager.services",)
    offenders: list[str] = []

    for path in sorted(_INTEGRATIONS.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: tuple[str, ...] = ()
            if isinstance(node, ast.Import):
                imported = tuple(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = (node.module,)
            for module in imported:
                if module.startswith(forbidden_prefixes):
                    rel = path.relative_to(_SRC)
                    offenders.append(f"{rel}:{node.lineno}: {module}")

    assert not offenders, (
        "integrations/ is the adapter layer and must not import services/:\n  "
        + "\n  ".join(offenders)
        + "\n\nPass what the adapter needs in from the composition root instead."
    )


# --------------------------------------------------------------------------
# (b) services/ must stay Rich- and Typer-free
# --------------------------------------------------------------------------


def _service_modules() -> list[str]:
    """Every importable module under services/, as dotted names.

    Package `__init__.py` files map to the package itself (`...services.events`)
    rather than being skipped: a package's `__init__` is its public surface and
    is exactly where a convenience re-export would smuggle Rich back in.
    """
    modules: list[str] = []
    for path in sorted(_SERVICES.rglob("*.py")):
        parts = list(path.relative_to(_SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules.append(".".join(parts))
    return modules


def _probe(modules: list[str]) -> str:
    """Import `modules` in a clean interpreter; return leaked top-level names.

    A subprocess is required because this test process has already imported
    Rich (via other test modules), so its own `sys.modules` proves nothing.

    `PYTHONPATH` is pinned to `_SRC` -- the tree `_service_modules()` just
    enumerated -- because a bare subprocess inherits none of pytest's
    `pythonpath = ["src"]` and would import whatever `chart_manager` happens
    to be installed in `sys.executable`'s environment instead. Those are the
    same directory in a plain checkout, and different ones in a git worktree,
    where the installed editable package still points at the main checkout.
    The failure mode is silent for existing modules and a confusing
    `ModuleNotFoundError` for a newly added one: the scan lists a file the
    probe cannot import. Enumerating one tree and importing another is not a
    property worth preserving.
    """
    script = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in modules)
        + f"leaked = sorted({{m.split('.')[0] for m in sys.modules}} & set({_FORBIDDEN_ROOTS!r}))\n"
        "print(','.join(leaked))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_SRC)},
    )
    if proc.returncode != 0:
        pytest.fail(
            f"importing the services layer failed:\nmodules: {', '.join(modules)}\n{proc.stderr}"
        )
    return proc.stdout.strip()


def test_service_modules_are_discoverable() -> None:
    """Guard the guard: an empty sweep would make the next test vacuously pass."""
    modules = _service_modules()
    assert len(modules) > 20, f"suspiciously few service modules found: {modules}"
    assert "chart_manager.services.helmrelease.wire" in modules
    assert "chart_manager.services.manifest_validation.app" in modules


def test_no_service_module_imports_rich_or_typer() -> None:
    """The service layer must be usable where there is no terminal.

    Rendering belongs to the surface: Rich widgets live in
    `cli/helmrelease_render.py`, `cli/validate_render.py` and
    `cli/validate_progress.py`; services narrate through injected callbacks
    (`services/progress.py`, `services/manifest_validation/progress.py`) and return
    plain result objects plus versioned wire projections
    (`services/*/wire.py`).

    Strategy: one subprocess importing every service module (~0.5s), which is
    all the passing case needs. Only on failure do we pay for a per-module
    re-probe, so the report still names the exact file that regressed rather
    than the whole layer.
    """
    modules = _service_modules()
    leaked = _probe(modules)
    if not leaked:
        return

    culprits = {m: found for m in modules if (found := _probe([m]))}
    detail = "\n".join(f"  {m} -> {found}" for m, found in sorted(culprits.items()))
    pytest.fail(
        f"services/ leaked {leaked} into sys.modules.\n"
        f"Modules responsible:\n{detail}\n\n"
        "Move the terminal rendering into cli/ and hand the service a "
        "callback or a plain result object instead."
    )


# --------------------------------------------------------------------------
# (c) process exit belongs to the surface layer
# --------------------------------------------------------------------------


def _process_exit_at(node: ast.AST) -> tuple[int, str] | None:
    """Return (line, label) if `node` terminates the interpreter, else None."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "sys" and node.attr == "exit":
            return node.lineno, "sys.exit"
        if node.value.id == "os" and node.attr == "_exit":
            return node.lineno, "os._exit"
    if (
        isinstance(node, ast.ImportFrom)
        and node.module == "sys"
        and any(alias.name == "exit" for alias in node.names)
    ):
        return node.lineno, "from sys import exit"
    # Bare exit()/quit() are site builtins -- not always present in an
    # embedded interpreter, and a process kill wherever they are.
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("exit", "quit")
    ):
        return node.lineno, f"{node.func.id}()"
    return None


def _non_surface_modules() -> list[Path]:
    """Every .py file under chart_manager/ that is not part of a surface."""
    return [
        path
        for path in sorted(_PKG.rglob("*.py"))
        if not any(path.is_relative_to(allowed) for allowed in _EXIT_ALLOWED_DIRS)
    ]


def test_non_surface_modules_are_discoverable() -> None:
    """Guard the guard: prove the scan actually has files to look at."""
    paths = _non_surface_modules()
    assert len(paths) > 30, f"suspiciously few non-surface modules: {len(paths)}"
    assert _PKG / "composition.py" in paths
    # Cluster workflows are grouped by subject but remain separate services.
    # The canaries ensure both the persistent development converge engine and
    # fail-fast ephemeral test service remain visible to the layer scan.
    clusters = _PKG / "services" / "clusters"
    assert clusters / "development" / "service.py" in paths
    assert clusters / "ephemeral.py" in paths
    assert clusters / "bootstrap.py" in paths


def test_no_process_exit_outside_cli() -> None:
    """Only the surface layer may terminate the process.

    A service that calls `sys.exit` cannot be reused by an HTTP handler, a
    Slack listener, or a long-lived worker -- it takes the whole process down
    instead of returning a result the caller can turn into a status code.
    Services raise `ChartManagerError` (or return a result carrying an
    `exit_code`, as `ManifestValidationService` does via `RunOutcome`); `cli/main.py` and
    `cli/validate.py` are the only places that translate that into an exit.
    """
    offenders: list[str] = []
    for path in _non_surface_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            found = _process_exit_at(node)
            if found is not None:
                line, label = found
                rel = path.relative_to(_PKG.parent.parent)
                offenders.append(f"  {rel}:{line}: {label}")

    assert not offenders, (
        "process exit is a surface concern, but found it outside cli/:\n"
        + "\n".join(offenders)
        + "\n\nRaise a ChartManagerError (or return a result carrying an "
        "exit code) and let the surface decide how to terminate."
    )
