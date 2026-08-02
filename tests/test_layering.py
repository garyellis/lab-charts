"""Executable form of the layer diagram.

The diagram this file encodes:

    cli/surfaces -> services -> api + domain + integrations
                             domain -> api + plumbing
                                api -> plumbing (pure helpers only)

Read the arrows as guidance, not as permission. `integrations/` sits on the
same rank as `api/` and `domain/` because services depend on all three, not
because an adapter should reach for an authored API type: adapters are handed
resolved service inputs, which is what keeps `Helm` ignorant of how a
`ChartLifecycle` is spelled.

`domain/` is a top-level package, so the diagram and the directory tree agree.
Membership is decided by what a module *is* -- policy and algorithms over
`api/` models plus the schemas this project does not own (`Chart.yaml`,
`Chart.lock`) -- not by who imports it.

Six invariants keep `cli/` a thin surface, so the same capabilities can be
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

  (d) `chart_manager.api` owns the authored YAML contracts and depends on
      nothing but the standard library, Pydantic, and pure lexical helpers.
      An engineer reviewing the accepted YAML reads one version module and is
      done; nothing about loading, resolution, execution or display can hide
      in there. Six checks under "(d)" below.

  (e) `chart_manager.domain` may not import `services/`, `integrations/` or
      `cli/`. Services compose domain policy; the reverse is a cycle waiting
      to happen, and it is how `domain/` acquired its previous
      "lives here because of who calls it" membership rule.
      `test_domain_imports_only_api_plumbing_and_the_standard_library` below,
      plus the TID251 table in `src/chart_manager/domain/.ruff.toml`.

  (f) A surface *obtains* a service from the composition root; it never
      constructs one, and it never reads `Settings` to do so. This is the
      one rule no import check can see: `ChartCatalogService` is imported
      legitimately in three layers, and the bypass was a call, not an
      import. It went unnoticed for exactly that reason, and the predictor
      was mechanical -- every service whose only dependency was
      `Settings` + `root` got built inline, every service needing an adapter
      went through `Container` -- so each bypass also built a second
      `Settings`, which is what made `Container(settings=...)` unreachable
      from the only surface that exists. Two checks under "(f)" below.

Why (c), (d) and (e) are tests and not just banned-api entries: ruff reports
every banned-api violation under the single code TID251, and per-file-ignores
are per-code. Rule (a) needs TID251 *lifted* inside `services/` (services are
supposed to wrap adapters), while rule (c) needs it *enforced* there -- that
is exactly where a stray `sys.exit` would do the most damage. One code cannot
be both, so (c) is checked here instead of weakening (a). Rule (d) hits the
same wall from the other side: banned-api is declared globally, so banning
`chart_manager.services` to protect `api/` also bans it for `cli/`, where it
is the whole point -- 92 legitimate imports, measured. Rule (e) needs the same
ban, but only under one directory, so it is declared in a nested `.ruff.toml`
that ruff resolves per file; the scan below is the copy that does not depend
on ruff's config discovery. The one arrow TID251 carries for free everywhere
is `-/-> integrations`: that ban is global, and `api/`, `domain/` and `cli/`
are all deliberately absent from the lift table in pyproject.toml.

One more invariant -- versioned wire contracts live in `services/*/wire.py`,
never in `cli/` -- is enforced separately in `tests/test_wire_contracts.py`,
because it is about dict literals rather than imports and so is invisible to
both TID251 and the scans here.
"""

from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_PKG = _SRC / "chart_manager"
_API = _PKG / "api"
_DOMAIN = _PKG / "domain"
_SERVICES = _PKG / "services"
_PLUMBING = _PKG / "plumbing"
_INTEGRATIONS = _PKG / "integrations"
_CLI = _PKG / "cli"

#: Only the surface layer may terminate the process.
_EXIT_ALLOWED_DIRS = (_CLI,)

#: Import either of these and a headless surface has a TUI in its address space.
_FORBIDDEN_ROOTS = ("rich", "typer")

# Chart concepts are domain policy, not generic plumbing.
# `graph.py` and `spec.py` no longer exist anywhere -- their contents split
# across `api/lifecycle/v1alpha1.py`, `domain/lifecycle_policy.py` and
# `manifest_validation/namespaces.py`. They stay on this ban list regardless:
# it names spellings that may not appear under `plumbing/`, and a future
# `plumbing/spec.py` would be exactly the violation it always was.
_DOMAIN_MODULES_FORBIDDEN_IN_PLUMBING = {
    "chart_deps.py",
    "charts.py",
    "cluster_tests.py",
    "graph.py",
    "install_plan.py",
    "lifecycle_policy.py",
    "local_resources.py",
    "spec.py",
}


def _package_of(path: Path) -> str:
    """The dotted package a module file lives in, e.g. `chart_manager.api.local`."""
    return ".".join(path.relative_to(_SRC).parent.parts)


def _imports_in(source: str, label: str, package: str = "") -> list[tuple[int, str]]:
    """Every module `source` imports, as `(line, absolute dotted name)`.

    Relative imports are resolved against `package`, so a scan cannot be
    slipped past by spelling the escape `from ...services import x`.

    Parsing rather than text-matching matters here: `plumbing/names.py` names
    `chart_manager.api.local.v1alpha1` in its docstring precisely to explain
    why the rule lives in plumbing, and a `grep` would call that a violation.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".") if package else []
            base = ".".join(parts[: len(parts) - node.level + 1]) if node.level else ""
            absolute = ".".join(part for part in (base, node.module) if part)
            if absolute:
                found.append((node.lineno, absolute))
    return found


def _imports_matching(directory: Path, prefixes: tuple[str, ...]) -> list[str]:
    """Every import under `directory` whose target starts with one of `prefixes`."""
    offenders: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        label = str(path.relative_to(_SRC))
        for lineno, module in _imports_in(
            path.read_text(encoding="utf-8"), label, _package_of(path)
        ):
            if module.startswith(prefixes):
                offenders.append(f"{label}:{lineno}: {module}")
    return offenders


def test_chart_domain_modules_stay_out_of_plumbing() -> None:
    """Keep chart models and policy in domain, not generic utilities."""
    misplaced = sorted(
        path.name
        for path in _PLUMBING.glob("*.py")
        if path.name in _DOMAIN_MODULES_FORBIDDEN_IN_PLUMBING
    )
    assert not misplaced, (
        "chart-domain modules belong in chart_manager/domain, "
        f"not plumbing: {', '.join(misplaced)}"
    )

    # `lifecycle_policy.py` is here because the capability gate and the profile
    # lookup were split out of the authored models when they moved to
    # `api/lifecycle/v1alpha1.py`: the shape is a contract, deciding whether a
    # capability is usable and phrasing the failure is policy. It must not
    # drift back -- and it must not drift *up* into `services/` either, which
    # is where `require_validation` and `require_cluster_test` used to sit
    # while their sibling `require_cluster_test_profile` sat down here.
    expected = {
        "chart_deps.py",
        "charts.py",
        "cluster_tests.py",
        "install_plan.py",
        "lifecycle_policy.py",
        "local_resources.py",
    }
    actual = {path.name for path in _DOMAIN.glob("*.py") if path.name != "__init__.py"}
    assert expected <= actual, (
        f"missing from domain: {sorted(expected - actual)} -- these are "
        "chart policy, not authored contract and not generic plumbing"
    )


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

    # `namespaces.py` holds `resolve_namespace`, split out of the authored
    # validation spec when that moved to `api/lifecycle/v1alpha1.py`. It is a
    # leaf on purpose: putting it on the compiler would make `planner.py`
    # drag in the helm/kubeconform/kyverno adapters.
    validation_domain = _SERVICES / "manifest_validation"
    expected = {"models.py", "namespaces.py", "paths.py"}
    actual = {path.name for path in validation_domain.glob("*.py") if path.name != "__init__.py"}
    assert expected <= actual, (
        f"missing from services/manifest_validation: {sorted(expected - actual)} -- "
        "these interpret the authored spec and stay on the service side of api/"
    )


def test_plumbing_does_not_import_domain_or_validation_policy() -> None:
    """Generic plumbing may not depend on chart or validation policy."""
    offenders = _imports_matching(
        _PLUMBING,
        (
            "chart_manager.domain",
            "chart_manager.services.manifest_validation",
        ),
    )

    assert not offenders, (
        "generic plumbing must not import domain or service policy:\n  " + "\n  ".join(offenders)
    )


def test_domain_modules_are_discoverable() -> None:
    """Guard the guard: an empty sweep would make the next test vacuously pass."""
    paths = sorted(_DOMAIN.rglob("*.py"))
    assert len(paths) > 5, f"suspiciously few domain modules found: {paths}"
    assert _DOMAIN / "install_plan.py" in paths
    assert _DOMAIN / "lifecycle_policy.py" in paths


#: What `chart_manager.domain` may import, beyond the standard library.
#: An allowlist rather than a ban list, so a *new* top-level package is out of
#: bounds for `domain/` on the day it is created, the same way the TID251 lift
#: table in pyproject.toml defaults a new package to "no adapters".
#:
#: `chart_manager.settings` is on the list and `api/` is denied it, and the
#: asymmetry is the point: a contract must describe what a YAML file may say
#: without knowing which repository it was found in, while resolving
#: `charts/<name>/Chart.yaml` is most of what domain does. `yaml` and
#: `pydantic` are here for the same reason -- turning bytes into a validated
#: document is the loading boundary domain owns, which is exactly the work
#: `api/` is forbidden from doing.
_DOMAIN_ALLOWED_IMPORTS = frozenset(
    {
        "chart_manager.api",
        "chart_manager.domain",
        "chart_manager.plumbing",
        "chart_manager.settings",
        "pydantic",
        "yaml",
    }
)


def test_domain_imports_only_api_plumbing_and_the_standard_library() -> None:
    """Domain is below services: nothing above it may be imported from here.

    This is the rule whose absence produced the previous membership criterion,
    where a policy function lived in `domain/` because a domain algorithm
    happened to call it, while its two siblings lived in `services/` because
    only services called them. With the direction enforced, "what is it?" is
    the only question left to answer, and the answer is stable.

    `src/chart_manager/domain/.ruff.toml` bans `chart_manager.services` and
    `chart_manager.integrations` under TID251, which reports them at lint
    time. This scan is the copy that does not depend on ruff's nested-config
    discovery, and being an allowlist it also covers `cli/`, the composition
    root, and whatever gets added next.
    """
    offenders: list[str] = []
    for path in sorted(_DOMAIN.rglob("*.py")):
        label = str(path.relative_to(_SRC))
        for lineno, module in _imports_in(
            path.read_text(encoding="utf-8"), label, _package_of(path)
        ):
            root = module.split(".")[0]
            if root in sys.stdlib_module_names:
                continue
            if any(
                module == allowed or module.startswith(allowed + ".")
                for allowed in _DOMAIN_ALLOWED_IMPORTS
            ):
                continue
            offenders.append(f"{label}:{lineno}: {module}")

    assert not offenders, (
        "domain/ sits below services/ and may import only api/, plumbing/, "
        "settings and the standard library:\n  "
        + "\n  ".join(offenders)
        + "\n\nTake what the algorithm needs as a parameter instead -- "
        "DependencyResolver's ClusterTestLoader callable is the pattern."
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
    arguments, wired from `domain/chart_deps` in
    `chart_manager.composition`, so the helm wrapper never has to know how a
    Chart.lock is parsed.
    """
    offenders = _imports_matching(_INTEGRATIONS, ("chart_manager.services",))

    assert not offenders, (
        "integrations/ is the adapter layer and must not import services/:\n  "
        + "\n  ".join(offenders)
        + "\n\nPass what the adapter needs in from the composition root instead."
    )


# --------------------------------------------------------------------------
# (b) services/ must stay Rich- and Typer-free
# --------------------------------------------------------------------------


def _modules_under(directory: Path) -> list[str]:
    """Every importable module under `directory`, as dotted names.

    Package `__init__.py` files map to the package itself (`...services.events`)
    rather than being skipped: a package's `__init__` is its public surface and
    is exactly where a convenience re-export would smuggle Rich back in.
    """
    modules: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        parts = list(path.relative_to(_SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules.append(".".join(parts))
    return modules


def _probe(modules: list[str], forbidden: tuple[str, ...] = _FORBIDDEN_ROOTS) -> str:
    """Import `modules` in a clean interpreter; return the `forbidden` names loaded.

    Matching is by dotted prefix, so `chart_manager.integrations` reports the
    layer that leaked rather than each of its modules, and the caller can
    re-probe one module at a time to name the culprit.

    A subprocess is required because this test process has already imported
    Rich (via other test modules), so its own `sys.modules` proves nothing.

    `PYTHONPATH` is pinned to `_SRC` -- the tree `_modules_under()` just
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
        + f"forbidden = {tuple(forbidden)!r}\n"
        "leaked = sorted({f for m in sys.modules for f in forbidden "
        "if m == f or m.startswith(f + '.')})\n"
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
            "importing this layer in a clean interpreter failed:\n"
            f"modules: {', '.join(modules)}\n{proc.stderr}"
        )
    return proc.stdout.strip()


def test_service_modules_are_discoverable() -> None:
    """Guard the guard: an empty sweep would make the next test vacuously pass."""
    modules = _modules_under(_SERVICES) + _modules_under(_DOMAIN)
    assert len(modules) > 20, f"suspiciously few service modules found: {modules}"
    assert "chart_manager.services.helmrelease.wire" in modules
    assert "chart_manager.services.manifest_validation.app" in modules
    assert "chart_manager.domain.lifecycle_policy" in modules


def test_no_service_module_imports_rich_or_typer() -> None:
    """The service layer must be usable where there is no terminal.

    Rendering belongs to the surface: Rich widgets live in
    `cli/helmrelease_render.py`, `cli/validate_render.py` and
    `cli/validate_progress.py`; services narrate through injected callbacks
    (`services/progress.py`, `services/manifest_validation/progress.py`) and return
    plain result objects plus versioned wire projections
    (`services/*/wire.py`).

    `domain/` is swept alongside `services/`: it is further from the terminal,
    not closer, so the same rule applies with more force.

    Strategy: one subprocess importing every service module (~0.5s), which is
    all the passing case needs. Only on failure do we pay for a per-module
    re-probe, so the report still names the exact file that regressed rather
    than the whole layer.
    """
    modules = _modules_under(_SERVICES) + _modules_under(_DOMAIN)
    leaked = _probe(modules)
    if not leaked:
        return

    culprits = {m: found for m in modules if (found := _probe([m]))}
    detail = "\n".join(f"  {m} -> {found}" for m, found in sorted(culprits.items()))
    pytest.fail(
        f"services/ + domain/ leaked {leaked} into sys.modules.\n"
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


# --------------------------------------------------------------------------
# (f) a surface obtains services from the composition root
# --------------------------------------------------------------------------

#: Class-name suffixes that mean "this is a service-layer collaborator".
#: Deliberately a naming rule rather than an import graph: the check has to
#: fire on `SomeService(...)` written in `cli/` whichever module it came from,
#: and has to stay quiet on `_container().some_service(...)`, which differs
#: only in case. Both properties fall out of matching the *called* name.
_CONSTRUCTION_SUFFIXES = ("Service", "Resolver", "Exporter")

#: `module.py::ClassName` pairs `cli/` may construct anyway.
#:
#: Empty, and the intent is that it stays empty -- there is no service a
#: surface can build better than the composition root can. If one ever earns
#: an entry, write the reason on the line above it: an allowlist whose
#: members carry no justification is indistinguishable from a suppressed
#: failure, and the next reader cannot tell which it is.
_CONSTRUCTION_ALLOWLIST: frozenset[str] = frozenset()

#: `cli/` modules permitted to call `Settings()` directly.
#:
#: Exactly one, and it is not service construction. `main.py`'s root callback
#: has to read `--config` into `settings.set_config_file` and then resolve
#: `--root`'s fallback and the logging configuration *before* any command
#: body -- and therefore before any `Container` -- exists. That is process
#: bootstrap, which is the surface's own job; every other former caller was
#: building a service and has been routed through `container().settings`.
_SETTINGS_ALLOWED_MODULES = frozenset({"main.py"})


def _called_names(source: str, label: str) -> list[tuple[int, str]]:
    """Every call in `source` as (line, called name).

    `Name(...)` reports `Name` and `mod.Name(...)` reports `Name`, so the
    rules below read the same whether a module imported a symbol or reached
    it through its package. Anything not called through a name (a subscript,
    a lambda) is not a construction site and is skipped.
    """
    called: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.append((node.lineno, node.func.id))
        elif isinstance(node.func, ast.Attribute):
            called.append((node.lineno, node.func.attr))
    return called


def _construction_offenders(
    source: str,
    label: str,
    allowlist: frozenset[str] = _CONSTRUCTION_ALLOWLIST,
) -> list[str]:
    """Service-layer objects constructed in `source`, as reportable strings."""
    return [
        f"  {label}:{line}: {name}(...)"
        for line, name in _called_names(source, label)
        if name.endswith(_CONSTRUCTION_SUFFIXES) and f"{label}::{name}" not in allowlist
    ]


def _settings_offenders(source: str, label: str) -> list[str]:
    """Direct `Settings()` constructions in `source`."""
    return [
        f"  {label}:{line}: Settings()"
        for line, name in _called_names(source, label)
        if name == "Settings"
    ]


def _cli_modules() -> list[Path]:
    """Every .py file under `cli/`."""
    return sorted(_CLI.rglob("*.py"))


def test_cli_modules_are_discoverable() -> None:
    """Guard the guard: an empty sweep would make both (f) checks vacuous."""
    paths = _cli_modules()
    assert len(paths) > 10, f"suspiciously few cli modules: {len(paths)}"
    assert _CLI / "_container.py" in paths
    # The four modules that used to construct services inline. If one is
    # renamed away, the scan must be updated deliberately rather than
    # quietly losing coverage of the exact files this rule was written for.
    for name in ("chart.py", "local.py", "plan.py", "validate.py"):
        assert _CLI / name in paths


def test_no_cli_module_constructs_a_service() -> None:
    """A surface asks the composition root for a capability; it never builds one.

    This is the container-bypass equivalent of TID251, and it exists because
    TID251 cannot see it. Importing `ChartCatalogService` into `cli/` is a
    legal import from a legal layer; calling it is what puts the decision
    "which chart directory is this repository's" at the surface, where every
    surface then has to make it again -- and where it silently disagreed with
    what `Container` was configured with.

    The fix is always the same shape: add a factory to `Container` and call
    `_container().the_factory(root)`. The lowercase factory name is why the
    match is on the exact capitalized suffix.
    """
    offenders: list[str] = []
    for path in _cli_modules():
        offenders += _construction_offenders(path.read_text(encoding="utf-8"), path.name)

    assert not offenders, (
        "cli/ must obtain services from the composition root, but found "
        "direct constructions:\n" + "\n".join(offenders) + "\n\n"
        "Add a factory to composition.Container and call "
        "_container().<factory>(...) instead."
    )


def test_only_the_cli_entry_point_constructs_settings() -> None:
    """Configuration enters through the container, not through each call site.

    `Container` has taken a `Settings` since it was written, but the seam was
    dead while every command built its own: injecting one changed nothing for
    the services a surface constructed itself. Reading `container().settings`
    -- which is what the two remaining settings-only free functions
    (`resolve_chart_target`, `discover_dashboards`) do -- keeps one
    configuration per invocation instead of one per call site.
    """
    offenders: list[str] = []
    for path in _cli_modules():
        if path.name in _SETTINGS_ALLOWED_MODULES:
            continue
        offenders += _settings_offenders(path.read_text(encoding="utf-8"), path.name)

    assert not offenders, (
        "only cli/main.py may construct Settings (process bootstrap), but "
        "found:\n" + "\n".join(offenders) + "\n\n"
        "Read container().settings instead, so an injected Settings reaches "
        "this call site too."
    )


_CONSTRUCTION_LEAKS = {
    "the-catalog-bypass-this-rule-was-written-for": (
        "entries = ChartCatalogService(root, charts_dir=Settings().charts_dir).list_entries()"
    ),
    "a-domain-resolver-built-from-a-setting": (
        "return LocalTargetResolver(root, local_config=Settings().local_config).resolve(target)"
    ),
    "an-adapter-facing-exporter": "exporter = GrafanaExporter(kubectl=Kubectl(runner))",
    "the-same-thing-reached-through-its-package": "svc = impact.LifecycleImpactService(root)",
}

_CONSTRUCTION_NON_LEAKS = {
    "the-container-factory-that-replaced-it": "entries = _container().chart_catalog_service(root)",
    "a-container-factory-spelled-bare": "svc = container().impact_service(root)",
    "a-type-annotation-naming-the-same-class": "def f(root: Path) -> LifecycleImpactService: ...",
    "an-import-of-the-class-for-that-annotation": (
        "from chart_manager.services.chart_catalog import ChartCatalogService"
    ),
    "a-request-object-the-surface-does-own": "req = ExportRequest(uid=uid, release=release)",
}


def test_the_construction_rule_fires_on_each_synthetic_leak() -> None:
    """Positive control: every bypass shape this rule replaced, caught."""
    missed = [
        name
        for name, source in _CONSTRUCTION_LEAKS.items()
        if not _construction_offenders(source, name)
    ]
    assert not missed, f"the container-bypass rule silently allows: {missed}"


def test_the_construction_rule_stays_quiet_on_going_through_the_container() -> None:
    """Negative control: the fixed spelling differs from the broken one by case."""
    noisy = {
        name: found
        for name, source in _CONSTRUCTION_NON_LEAKS.items()
        if (found := _construction_offenders(source, name))
    }
    assert not noisy, f"the container-bypass rule flags correct surface code: {noisy}"


def test_the_construction_allowlist_exempts_exactly_what_it_names() -> None:
    """The allowlist is empty today; prove the mechanism still works.

    An allowlist nothing exercises is an allowlist that silently stops
    filtering, and the next person to need one finds out the hard way.
    """
    source = _CONSTRUCTION_LEAKS["an-adapter-facing-exporter"]
    assert _construction_offenders(source, "grafana.py", frozenset({"grafana.py::Other"}))
    assert not _construction_offenders(
        source, "grafana.py", frozenset({"grafana.py::GrafanaExporter"})
    )


def test_the_settings_rule_fires_on_a_direct_construction() -> None:
    """Positive control, plus the negative one: reading the container is fine."""
    assert _settings_offenders("settings = Settings()", "chart.py")
    assert _settings_offenders("x = Settings().charts_dir", "grafana.py")
    assert not _settings_offenders("x = container().settings.charts_dir", "grafana.py")
    assert not _settings_offenders("def f(settings: Settings | None = None): ...", "_container.py")


# --------------------------------------------------------------------------
# (d) the configuration API boundary
# --------------------------------------------------------------------------

#: The authored contracts, and the root envelope each version module owns.
#: Consumers import the explicit version -- there are deliberately no
#: versionless re-exports, so a future `v1beta1` cannot silently change what
#: an existing consumer parses -- which makes these the canonical spellings.
_API_ROOT_MODELS = {
    "chart_manager.api.lifecycle.v1alpha1": ("ChartLifecycle",),
    "chart_manager.api.local.v1alpha1": ("LocalCluster", "LocalStack"),
}

#: Layers `api/` may not reach for, each with the reason it is out of bounds.
#: Used twice: statically, against the imports written in `api/`, and at
#: runtime, against what importing `api/` actually loads.
_API_FORBIDDEN_IMPORTS = {
    "chart_manager.domain": "interpreting a decoded document is the layer above",
    "chart_manager.services": "loading and interpretation happen after decode",
    "chart_manager.integrations": "adapters run commands; a contract describes text",
    "chart_manager.cli": "a contract must not know how it is rendered",
    "chart_manager.composition": "wiring adapters is the composition root's job",
    "chart_manager.settings": "settings are repository state, not authored shape",
    "rich": "a contract must be decodable where there is no terminal",
    "typer": "a contract must be decodable where there is no terminal",
    "yaml": "turning bytes into dicts is the loader's job, in services/",
}

#: The only non-stdlib, non-Pydantic imports `api/` may make. Both are pure
#: `str`/`Path` rules that import nothing but the standard library, touch no
#: filesystem, and raise `ValueError` -- so importing them cannot drag a layer
#: in behind them, and cannot make an authored field raise `SpecError`.
#: Spelled out module by module rather than as a `chart_manager.plumbing`
#: prefix: `plumbing.errors`, `plumbing.commands` and `plumbing.yaml_files`
#: are all in that package and none of them belong in a contract.
_API_ALLOWED_HELPERS = frozenset(
    {
        "chart_manager.plumbing.names",
        "chart_manager.plumbing.paths",
    }
)

#: Service diagnostics. An authored model that raises one of these has decided
#: how its own failure is reported, which is the loader's decision.
_SERVICE_ERRORS = frozenset({"SpecError", "ChartManagerError"})

#: What an `api/` module may raise. Pydantic turns a `ValueError` from a
#: validator into a `ValidationError` carrying the field path; anything else
#: escapes as itself and the loader can no longer describe where it happened.
_API_ALLOWED_RAISES = frozenset({"ValueError"})


def _api_import_verdict(module: str) -> str | None:
    """Why `api/` may not import `module`, or None if the import is allowed."""
    if module.split(".")[0] in sys.stdlib_module_names or module.split(".")[0] == "pydantic":
        return None
    if module == "chart_manager.api" or module.startswith("chart_manager.api."):
        return None
    if module in _API_ALLOWED_HELPERS:
        return None
    for banned, reason in _API_FORBIDDEN_IMPORTS.items():
        if module == banned or module.startswith(banned + "."):
            return reason
    return "not stdlib, Pydantic, or one of the pure helpers on the allowlist"


def test_api_modules_exist_and_expose_their_root_models() -> None:
    """Guard the guard: every check below is vacuous over an empty package.

    This is the positive half of the boundary -- the contracts exist, at the
    dotted paths the rest of the codebase is required to import them from.
    """
    for dotted, models in _API_ROOT_MODELS.items():
        path = _SRC.joinpath(*dotted.split(".")).with_suffix(".py")
        assert path.exists(), f"the canonical contract module is missing: {path}"
        module = importlib.import_module(dotted)
        missing = [name for name in models if not hasattr(module, name)]
        assert not missing, f"{dotted} no longer defines {', '.join(missing)}"

    # D6: the version is part of the import path. A versionless re-export
    # would let `from chart_manager.api.local import LocalCluster` keep
    # working across a version bump while quietly parsing something else.
    for dotted, models in _API_ROOT_MODELS.items():
        group = importlib.import_module(dotted.rsplit(".", 1)[0])
        leaked = [name for name in models if hasattr(group, name)]
        assert not leaked, (
            f"{group.__name__} re-exports {', '.join(leaked)}; import the explicit "
            f"version instead ({dotted}) so a future version cannot change what "
            "an existing consumer parses."
        )


def test_api_imports_only_stdlib_pydantic_and_pure_helpers() -> None:
    """A contract module describes accepted text and nothing else.

    The point of the package is that reviewing the accepted YAML means reading
    one version module. Every import out of `api/` is a thing the reviewer now
    also has to read, and a thing that can put behavior -- a filesystem probe,
    a subprocess, a settings lookup -- behind a field.
    """
    offenders: list[str] = []
    for path in sorted(_API.rglob("*.py")):
        label = str(path.relative_to(_SRC))
        for lineno, module in _imports_in(
            path.read_text(encoding="utf-8"), label, _package_of(path)
        ):
            reason = _api_import_verdict(module)
            if reason is not None:
                offenders.append(f"  {label}:{lineno}: {module} -- {reason}")

    assert not offenders, (
        "chart_manager/api/ owns the authored YAML contracts and may import only "
        "the standard library, Pydantic, and the pure helpers on "
        "_API_ALLOWED_HELPERS:\n" + "\n".join(offenders) + "\n\nKeep the shape in api/ and move "
        "loading, resolution and execution into services/. If a genuinely pure "
        "lexical rule is needed, put it in chart_manager/plumbing and add it to "
        "_API_ALLOWED_HELPERS, deliberately."
    )


def test_importing_the_api_loads_no_surface_service_or_adapter() -> None:
    """The static scan sees one file; this sees the whole transitive closure.

    A helper added to `_API_ALLOWED_HELPERS` that grows an import of its own
    passes the scan above and fails here, which is the case the allowlist is
    most likely to get wrong.

    Same strategy as the services probe: one subprocess for the passing case,
    per-module re-probes only when something leaked, so the report names the
    contract module that regressed.
    """
    modules = _modules_under(_API)
    forbidden = tuple(_API_FORBIDDEN_IMPORTS)
    leaked = _probe(modules, forbidden)
    if not leaked:
        return

    culprits = {m: found for m in modules if (found := _probe([m], forbidden))}
    detail = "\n".join(f"  {m} -> {found}" for m, found in sorted(culprits.items()))
    pytest.fail(
        f"importing chart_manager.api pulled in {leaked}.\n"
        f"Modules responsible:\n{detail}\n\n"
        "Authored contracts must be decodable in a process that has no "
        "terminal, no adapters and no services -- an editor plugin, a schema "
        "generator, a REST handler validating a request body."
    )


def test_plumbing_does_not_import_the_configuration_api() -> None:
    """`api/` imports plumbing, so the reverse edge is a cycle.

    `api/local/v1alpha1.py` calls `plumbing.names.dns_label` and
    `plumbing.paths.relative_path`. That direction is deliberate and is what
    keeps one definition of a DNS label for authored fields and for the stack
    name typed on the command line. Plumbing reaching back for an API type
    would make the generic utilities depend on a versioned contract, and the
    next version bump would be unable to move without moving them too.
    """
    offenders = _imports_matching(_PLUMBING, ("chart_manager.api",))

    assert not offenders, (
        "plumbing/ is generic and must not import the versioned contracts in api/:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe dependency runs api -> plumbing. Take the value the helper "
        "needs as a parameter instead of importing the model that holds it."
    )


def _raise_offenders(source: str, label: str) -> list[str]:
    """Every `raise` in `source` of something other than `_API_ALLOWED_RAISES`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = exc.id if isinstance(exc, ast.Name) else getattr(exc, "attr", "")
        if name in _API_ALLOWED_RAISES:
            continue
        why = (
            "a service diagnostic"
            if name in _SERVICE_ERRORS
            else "not a value error Pydantic can attribute to a field"
        )
        found.append(f"  {label}:{node.lineno}: raise {name or '<expression>'} -- {why}")
    return found


def test_api_validators_raise_only_value_errors() -> None:
    """A decode failure is a value being wrong, not a diagnostic being chosen.

    `SpecError` carries an exit code and a user-facing message, which means
    committing to how the failure is reported. `api/` raises `ValueError`;
    Pydantic attaches the field path and the loader in `services/` decides
    what the user sees. This is also what lets the same models be reused by a
    REST handler that must answer 400 rather than exit.

    The narrower question -- can `api/` even *import*
    `chart_manager.plumbing.errors`? -- is already answered by
    `test_api_imports_only_stdlib_pydantic_and_pure_helpers`, which allows two
    plumbing modules and that is not one of them. This check is about the
    raise itself, so it still fires if the exception arrives some other way.
    """
    offenders = [
        offender
        for path in sorted(_API.rglob("*.py"))
        for offender in _raise_offenders(
            path.read_text(encoding="utf-8"), str(path.relative_to(_SRC))
        )
    ]

    assert not offenders, (
        "authored models raise ValueError and let the loader translate it:\n"
        + "\n".join(offenders)
        + "\n\nRaise ValueError here and map it to a SpecError (or an HTTP "
        "status) in the layer that decided to read the document."
    )


# --------------------------------------------------------------------------
# (d) continued: authored resource envelopes live under api/
# --------------------------------------------------------------------------

#: Both halves of the Kubernetes-style document header. Requiring *both* is
#: what separates an authored envelope from a discriminated-union tag:
#: `ResolvedChartTarget.kind` is a `Literal["chart"]` selector on a resolved
#: in-memory value, and `HelmReleaseRef.api_version` is a field read back off
#: a cluster object. Neither is a document a person writes.
_ENVELOPE_API_VERSION = frozenset({"apiVersion", "api_version"})

#: Classes outside `api/` that legitimately declare the header as fields.
#: Empty, and the emptiness is the finding: `services/lifecycle/models.py`'s
#: `LifecyclePlan` is an execution-domain projection, not configuration API,
#: and since `services/lifecycle/wire.py` took over its emission it neither
#: declares nor emits `apiVersion`/`kind` at all. Anything added here is
#: keyed `path::ClassName` and needs a comment saying which external consumer
#: parses it and why it is not authored YAML.
_ENVELOPE_ALLOWLIST: frozenset[str] = frozenset()


def _envelope_classes(source: str, label: str) -> list[tuple[int, str]]:
    """Every class in `source` declaring an authored `apiVersion` + `kind` header."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.ClassDef):
            continue
        declared: set[str] = set()
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
                continue
            # The authored spelling is the alias when there is one:
            # `api_version: ... = Field(alias="apiVersion")`.
            alias = ""
            if isinstance(stmt.value, ast.Call):
                for keyword in stmt.value.keywords:
                    if keyword.arg == "alias" and isinstance(keyword.value, ast.Constant):
                        alias = str(keyword.value.value)
            declared.add(alias or stmt.target.id)
        if declared & _ENVELOPE_API_VERSION and "kind" in declared:
            found.append((node.lineno, node.name))
    return found


def _misplaced_envelopes(
    source: str, label: str, allowed: frozenset[str] = _ENVELOPE_ALLOWLIST
) -> list[str]:
    """Every authored envelope declared in `source` that is not allowlisted."""
    return [
        f"  {label}:{lineno}: {name}"
        for lineno, name in _envelope_classes(source, label)
        if f"{label}::{name}" not in allowed
    ]


def test_the_envelope_scan_finds_the_envelopes_that_exist() -> None:
    """Guard the guard: a detector that matches nothing would pass everywhere.

    Calibration against the real thing rather than synthetic sources alone --
    these three classes are the entire authored surface of the product.
    """
    found = {
        name
        for path in sorted(_API.rglob("*.py"))
        for _, name in _envelope_classes(
            path.read_text(encoding="utf-8"), str(path.relative_to(_SRC))
        )
    }
    assert found == {"ChartLifecycle", "LocalCluster", "LocalStack"}, (
        f"the envelope detector no longer recognizes the authored resources: {sorted(found)}"
    )


def test_authored_resource_envelopes_live_under_the_api_package() -> None:
    """A document a person writes is described in exactly one place.

    The failure this prevents is a second envelope growing where it is
    convenient -- next to the loader that reads it, or next to the service
    that consumes it -- so that "what YAML does this accept?" stops having a
    single answer and the two copies drift.
    """
    offenders = [
        offender
        for path in sorted(_PKG.rglob("*.py"))
        if not path.is_relative_to(_API)
        for offender in _misplaced_envelopes(
            path.read_text(encoding="utf-8"), str(path.relative_to(_SRC))
        )
    ]

    assert not offenders, (
        "authored resource envelopes belong in chart_manager/api/<group>/<version>.py:\n"
        + "\n".join(offenders)
        + "\n\nMove the model into the version module and import it from there. "
        "If it is a wire projection rather than authored YAML, add it to "
        "_ENVELOPE_ALLOWLIST with the consumer that parses it."
    )


# --------------------------------------------------------------------------
# (d) controls: the checks fire on real violations, and only on those
# --------------------------------------------------------------------------

_API_LEAKS = {
    "reaching-for-the-loader": "from chart_manager.domain.lifecycle_policy import load",
    "reaching-for-an-adapter": "from chart_manager.integrations.helm import Helm",
    "reaching-for-the-surface": "import chart_manager.cli.output",
    "reaching-for-settings": "from chart_manager.settings import Settings",
    "decoding-its-own-yaml": "import yaml",
    "rendering-its-own-errors": "from rich.console import Console",
    "a-plumbing-module-that-is-not-a-pure-rule": (
        "from chart_manager.plumbing.errors import SpecError"
    ),
    "an-escape-hatch-spelled-relatively": "from ...domain.lifecycle_policy import load",
}

_API_ALLOWED_SOURCES = {
    "the-standard-library": "import re\nfrom pathlib import Path",
    "pydantic": "from pydantic import Field, field_validator",
    "the-shared-base": "from chart_manager.api.base import StrictApiModel",
    "a-pure-lexical-rule": "from chart_manager.plumbing.names import dns_label",
    "a-pure-path-rule": "from chart_manager.plumbing.paths import ensure_relative",
}


def test_the_api_import_rule_fires_on_each_synthetic_leak() -> None:
    """Positive control: every shape the rule claims to catch, caught."""
    missed = [
        name
        for name, source in _API_LEAKS.items()
        if not [
            module
            for _, module in _imports_in(source, name, "chart_manager.api.lifecycle")
            if _api_import_verdict(module) is not None
        ]
    ]
    assert not missed, f"the api import rule silently allows: {missed}"


def test_the_api_import_rule_stays_quiet_on_what_a_contract_legitimately_needs() -> None:
    """Negative control: a rule that flags Pydantic is a rule people delete."""
    noisy = {
        name: [
            f"{module}: {verdict}"
            for _, module in _imports_in(source, name, "chart_manager.api.lifecycle")
            if (verdict := _api_import_verdict(module)) is not None
        ]
        for name, source in _API_ALLOWED_SOURCES.items()
    }
    noisy = {name: found for name, found in noisy.items() if found}
    assert not noisy, f"the api import rule flags legitimate contract code: {noisy}"


_ENVELOPE_LEAKS = {
    "a-new-authored-resource-next-to-its-loader": (
        "class LocalRegistry(StrictApiModel):\n"
        '    api_version: Literal["local.chartmanager.io/v1alpha1"] = Field(alias="apiVersion")\n'
        '    kind: Literal["LocalRegistry"]\n'
    ),
    "the-same-thing-spelled-without-an-alias": (
        "class LocalRegistry(BaseModel):\n    apiVersion: str\n    kind: str\n"
    ),
}

_ENVELOPE_NON_LEAKS = {
    "a-discriminated-union-tag": (
        'class ResolvedChartTarget(BaseModel):\n    kind: Literal["chart"] = "chart"\n'
        "    name: str\n    path: Path\n"
    ),
    "a-cluster-object-read-back-by-an-adapter": (
        "@dataclass(frozen=True)\nclass HelmReleaseRef:\n    name: str\n    api_version: str\n"
    ),
    "a-projection-that-only-emits-the-header": (
        "@dataclass(frozen=True)\nclass LifecyclePlan:\n    chart: str\n\n"
        '    def to_dict(self):\n        return {"apiVersion": V, "kind": "LifecyclePlan"}\n'
    ),
}


def test_the_envelope_rule_fires_on_each_synthetic_leak() -> None:
    """Positive control: both spellings of the header, caught."""
    missed = [
        name for name, source in _ENVELOPE_LEAKS.items() if not _misplaced_envelopes(source, name)
    ]
    assert not missed, f"the envelope rule silently allows: {missed}"


def test_the_envelope_rule_stays_quiet_on_kind_fields_that_are_not_envelopes() -> None:
    """Negative control: `kind` is a common field name and must not be enough."""
    noisy = {
        name: found
        for name, source in _ENVELOPE_NON_LEAKS.items()
        if (found := _misplaced_envelopes(source, name))
    }
    assert not noisy, f"the envelope rule flags legitimate models: {noisy}"


def test_the_envelope_allowlist_exempts_exactly_what_it_names() -> None:
    """The allowlist is empty today; prove the mechanism still works.

    An allowlist nothing exercises is an allowlist that silently stops
    filtering, and the next person to need one finds out the hard way.
    """
    source = _ENVELOPE_LEAKS["the-same-thing-spelled-without-an-alias"]
    assert _misplaced_envelopes(source, "wire.py", frozenset({"wire.py::Other"}))
    assert not _misplaced_envelopes(source, "wire.py", frozenset({"wire.py::LocalRegistry"}))


_RAISE_LEAKS = {
    "raising-the-service-diagnostic": 'raise SpecError("metadata.name is required")',
    "raising-it-qualified": 'raise errors.SpecError("metadata.name is required")',
    "raising-anything-else": 'raise RuntimeError("unreachable")',
}


def test_the_raise_rule_fires_on_each_synthetic_leak() -> None:
    """Positive control, and a negative one: plain `raise ValueError` is fine."""
    missed = [name for name, source in _RAISE_LEAKS.items() if not _raise_offenders(source, name)]
    assert not missed, f"the raise rule silently allows: {missed}"
    assert not _raise_offenders('raise ValueError("release.chart must be non-empty")', "ok")
