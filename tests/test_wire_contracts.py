"""Executable form of design commitment 5: wire contracts live in `services/*/wire.py`.

A *wire contract* is the shape of a document some other program parses: a
`--output json` payload piped into `jq`, a matrix captured into a GitHub
Actions `strategy.matrix`. Whoever owns that shape owns a promise to an
external consumer, and there must be exactly one owner -- otherwise a REST
endpoint, a Slack app, and the CLI each grow their own almost-identical copy
and drift.

Neither ruff's TID251 nor `tests/test_layering.py` can catch this. A dict
literal built in `cli/` imports nothing from `integrations/`, constructs no
Rich widget, and calls no `sys.exit`; it passes every existing gate while
quietly making the surface the owner of a contract. Ruff has no plugin system,
and no built-in rule expresses "this dict literal claims ownership of an
external shape", so the rule is an AST test. Two checks, both over `cli/`:

  (1) NO CONTRACT MARKER IN A SURFACE DICT. A dict literal may not carry a key
      whose only purpose is to let an external consumer pin a shape --
      `schema_version`, `schemaVersion`, `apiVersion`. Declaring a payload
      version in the surface *is* claiming ownership of the payload.

  (2) NO MATRIX DOCUMENT IN THE SURFACE. A dict literal may not carry a
      *list-valued* `include`/`exclude` key: that is the GitHub Actions
      `strategy.matrix` shape, whose contract is with GitHub. The list-valued
      qualifier is deliberate, so a plain `{"include": True}` option dict is
      not a matrix and does not trip the rule.

Both leaks fixed under design-doc 8.3 were exactly these two shapes, and
`test_the_rule_catches_the_leaks_that_actually_happened` replays them.

To satisfy the rule, move the payload to `services/<x>/wire.py` as a function
returning a plain dict and return it from the service; the surface keeps only
the encoder and the rendering, which are transport rather than contract.
`services/upgrader/wire.py` and `services/ci_wire.py` are worked examples --
both used to live in `cli/`.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
_PKG = _SRC / "chart_manager"

#: Surfaces. `cli/` is the only one today; a future REST or Slack surface is
#: guarded the day it is added by appending it here.
_SURFACE_DIRS = (_PKG / "cli",)

#: Keys that exist only so an external consumer can pin a payload's shape.
_CONTRACT_MARKER_KEYS = frozenset({"schema_version", "schemaVersion", "apiVersion"})

#: The GitHub Actions `strategy.matrix` document, flagged only when list-shaped.
_MATRIX_KEYS = frozenset({"include", "exclude"})


def _surface_modules() -> list[Path]:
    """Every .py file belonging to a surface."""
    return sorted(path for directory in _SURFACE_DIRS for path in directory.rglob("*.py"))


def _string_keyed_dicts(source: str, label: str) -> list[tuple[ast.Dict, dict[str, ast.expr]]]:
    """Every dict display carrying literal string keys, mapped to their values."""
    found: list[tuple[ast.Dict, dict[str, ast.expr]]] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if not isinstance(node, ast.Dict):
            continue
        keys = {
            key.value: value
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if keys:
            found.append((node, keys))
    return found


def _leaks(source: str, label: str) -> list[str]:
    """Every wire-contract leak in `source`, as `label:line: reason`."""
    found: list[str] = []
    for node, keys in _string_keyed_dicts(source, label):
        for key in sorted(_CONTRACT_MARKER_KEYS & keys.keys()):
            found.append(f"  {label}:{node.lineno}: declares {key!r}, a wire-contract marker")
        for key in sorted(_MATRIX_KEYS & keys.keys()):
            if isinstance(keys[key], ast.List | ast.ListComp):
                found.append(f"  {label}:{node.lineno}: builds the {key!r} matrix document CI reads")
    return sorted(found)


def _module_leaks(path: Path) -> list[str]:
    return _leaks(path.read_text(encoding="utf-8"), str(path.relative_to(_SRC)))


# --------------------------------------------------------------------------
# Guard the guards: the scan must find the constructs it means to check
# --------------------------------------------------------------------------


def test_surface_modules_are_discoverable() -> None:
    """An empty sweep would make the rule below vacuously pass."""
    paths = _surface_modules()
    assert len(paths) > 12, f"suspiciously few surface modules: {paths}"
    names = {path.name for path in paths}
    assert {"chart.py", "plan.py", "upgrade.py", "validate.py"} <= names


def test_the_scan_sees_the_dict_literals_it_means_to_check() -> None:
    """Both checks read string-keyed dict displays; prove some are found."""
    literals = [
        (path.name, node.lineno)
        for path in _surface_modules()
        for node, _ in _string_keyed_dicts(path.read_text(encoding="utf-8"), path.name)
    ]
    assert len(literals) >= 5, f"suspiciously few string-keyed dicts in cli/: {literals}"


# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def test_no_wire_contract_is_built_in_the_surface() -> None:
    """Documents other programs parse are owned by `services/*/wire.py`.

    To fix a failure here, move the payload into `services/<x>/wire.py` as a
    function returning a plain dict and return it from the service, leaving
    `cli/` holding only the encoder settings and the human-readable rendering.
    `services/upgrader/wire.py` and `services/ci_wire.py` are the two worked
    examples, both of which used to live in `cli/`.
    """
    offenders = [leak for path in _surface_modules() for leak in _module_leaks(path)]

    assert not offenders, (
        "wire contracts belong to services/, but the surface builds these:\n"
        + "\n".join(offenders)
        + "\n\nMove the payload to services/<x>/wire.py and return it from "
        "the service; keep only the encoder and the rendering in cli/."
    )


# --------------------------------------------------------------------------
# Calibration: the rule is proven against the leaks that really happened
# --------------------------------------------------------------------------

#: Transcribed from the commits that fixed them, rather than read back with
#: `git show <ref>^:<path>`: the CI job that runs `pytest -q` (`layering` in
#: .github/workflows/ci.yaml) checks out shallow, so a test shelling out to
#: git would pass locally and error there. The refs are recorded so the
#: originals stay one command away.
_HISTORICAL_LEAKS = {
    # 844e682^:src/chart_manager/cli/main.py:723 -> now services/ci_wire.py
    "cli/main.py built the GitHub Actions matrix": """
payload = {"include": [{"chart": e.chart, "profile": e.profile} for e in entries]}
typer.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
""",
    # f4ddbd8^:src/chart_manager/cli/upgrade.py:156 -> now services/upgrader/wire.py
    "cli/upgrade.py declared its own payload version": """
payload = {
    "schema_version": 1,
    "chart": raw.get("chart"),
    "outcome": outcome,
}
return _json_value(payload)
""",
}


def test_the_rule_catches_the_leaks_that_actually_happened() -> None:
    """Calibration against history, not against synthetic cases alone.

    A gate justified only by leaks it invented is unfalsifiable. These two are
    the real defects design-doc 8.3 moved out of `cli/`, and each is caught by
    one of the two checks -- which is also why the provenance/dataflow check
    the first draft carried was dropped: it added nothing on 2 of 2.
    """
    missed = [name for name, source in _HISTORICAL_LEAKS.items() if not _leaks(source, name)]
    assert not missed, f"the rule does not catch the leaks it exists for: {missed}"


# --------------------------------------------------------------------------
# Positive and negative controls: the rule fires, and only on real leaks
# --------------------------------------------------------------------------

_LEAKS = {
    "versioned-payload": '{"schema_version": 1, "chart": result.chart}',
    "camel-cased-version": '{"schemaVersion": 2, "panels": panels}',
    "kubernetes-style-document": '{"apiVersion": "v1", "kind": "ConfigMap"}',
    "github-matrix": '{"include": [{"chart": c} for c in charts]}',
    "github-matrix-exclusions": '{"exclude": [{"profile": "slow"}]}',
}


def test_the_rule_fires_on_each_synthetic_leak() -> None:
    """Positive control: every shape the rule claims to catch, caught."""
    missed = [name for name, source in _LEAKS.items() if not _leaks(source, name)]
    assert not missed, f"the rule silently allows these leak shapes: {missed}"


_ALLOWED = {
    "rendering-a-service-payload": (
        "typer.echo(json.dumps(upgrade_to_dict(result), sort_keys=True))"
    ),
    "a-result-objects-own-projection": "print(yaml.safe_dump(result.to_dict()))",
    "a-surface-local-style-table": '_STYLES = {"step": "bold", "warn": "yellow"}',
    "a-surface-local-option-table": '_HINTS = {"changed_files": "--changed-files"}',
    "encoder-settings-as-kwargs": '_DUMP = {"sort_keys": True, "separators": (",", ":")}',
    "an-option-dict-that-merely-says-include": '{"include": True, "exclude": False}',
}


def test_the_rule_stays_quiet_on_the_shapes_the_surface_legitimately_builds() -> None:
    """Negative control: a rule that flags every dict in `cli/` is unusable.

    The surface renders service-produced documents and builds option tables,
    style maps, and encoder kwargs constantly. Each case here is one of those,
    and each must stay silent or the gate becomes something people disable.
    """
    noisy = {name: leaks for name, source in _ALLOWED.items() if (leaks := _leaks(source, name))}
    assert not noisy, f"the rule flags legitimate surface code: {noisy}"
