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
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_PKG = _SRC / "chart_manager"
_SERVICES = _PKG / "services"
_PLUMBING = _PKG / "plumbing"

#: Only the surface layer may terminate the process.
_EXIT_ALLOWED_DIRS = (_PKG / "cli",)

#: Import either of these and a headless surface has a TUI in its address space.
_FORBIDDEN_ROOTS = ("rich", "typer")

# Chart concepts are service-domain policy, not generic plumbing.
_DOMAIN_MODULES_FORBIDDEN_IN_PLUMBING = {
    "chart_deps.py",
    "charts.py",
    "graph.py",
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
        "graph.py",
        "spec.py",
    }
    actual = {
        path.name
        for path in (_SERVICES / "domain").glob("*.py")
        if path.name != "__init__.py"
    }
    assert expected <= actual


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
    """
    script = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in modules)
        + f"leaked = sorted({{m.split('.')[0] for m in sys.modules}} & set({_FORBIDDEN_ROOTS!r}))\n"
        "print(','.join(leaked))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    if proc.returncode != 0:
        pytest.fail(
            "importing the services layer failed:\n"
            f"modules: {', '.join(modules)}\n{proc.stderr}"
        )
    return proc.stdout.strip()


def test_service_modules_are_discoverable() -> None:
    """Guard the guard: an empty sweep would make the next test vacuously pass."""
    modules = _service_modules()
    assert len(modules) > 20, f"suspiciously few service modules found: {modules}"
    assert "chart_manager.services.helmrelease.wire" in modules
    assert "chart_manager.services.validate.app" in modules


def test_no_service_module_imports_rich_or_typer() -> None:
    """The service layer must be usable where there is no terminal.

    Rendering belongs to the surface: Rich widgets live in
    `cli/helmrelease_render.py`, `cli/validate_render.py` and
    `cli/validate_progress.py`; services narrate through injected callbacks
    (`services/progress.py`, `services/validate/progress.py`) and return
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
    # `services/lab` became a package (models/access/drift/service); the
    # canary follows the converge engine to its new file. Pure move -- the
    # old assertion was not wrong, just pinned to a path that no longer exists.
    assert _PKG / "services" / "lab" / "service.py" in paths


def test_no_process_exit_outside_cli() -> None:
    """Only the surface layer may terminate the process.

    A service that calls `sys.exit` cannot be reused by an HTTP handler, a
    Slack listener, or a long-lived worker -- it takes the whole process down
    instead of returning a result the caller can turn into a status code.
    Services raise `ChartManagerError` (or return a result carrying an
    `exit_code`, as `ValidateApp` does via `RunOutcome`); `cli/main.py` and
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
