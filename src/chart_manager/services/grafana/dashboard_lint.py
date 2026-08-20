"""Lint Grafana dashboard JSON files for repo-wide quality rules.

Pure-Python, stdlib-only. Designed to be fast and to produce stable,
greppable output suitable for CI.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chart_manager.settings import DEFAULT_CHARTS_DIR, RepositoryLayout


@dataclass(frozen=True)
class Finding:
    """One lint violation: file, rule id, and message."""

    path: Path
    rule: str
    message: str

    def render(self) -> str:
        """Format as a single greppable `path: [rule] message` line."""
        return f"{self.path}: [{self.rule}] {self.message}"


_SHORT_RATE = re.compile(
    r"\b(rate|irate|increase)\s*\([^)]*\[(?:\d+s|[1-5]m)\]"
)
_MAX_DASHBOARD_BYTES = 900 * 1024
_SUPPORTED_URL = re.compile(r"^(?:https://|/|\$\{)")


def _iter_panels(dash: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield every panel, descending into row panels' nested `panels`."""
    def walk(panels: Any) -> Iterable[dict[str, Any]]:
        """Recurse one panel list, recursing again into row containers."""
        for p in panels or []:
            yield p
            if p.get("type") == "row" and p.get("panels"):
                yield from walk(p["panels"])

    yield from walk(dash.get("panels"))


def _iter_objects(node: Any) -> Iterable[dict[str, Any]]:
    """Yield every object in a dashboard tree for recursive safety checks."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_objects(value)


def rendered_configmap_name(path: Path) -> str:
    """Return the collision-resistant name used by the Helm template."""
    group = path.parent.name
    identity = f"{group}-{path.stem}"
    digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
    return f"grafana-dashboard-{identity[:35].rstrip('-')}-{digest}"


def lint_dashboard(path: Path) -> list[Finding]:
    """Lint one dashboard JSON file; invalid or unreadable JSON is R000.

    `UnicodeDecodeError` joins `JSONDecodeError` on the R000 arm: a binary
    file handed to `--path` is the same event as a malformed one -- "this is
    not dashboard JSON" -- and it reached the operator as a traceback until
    it was named here.
    """
    try:
        raw = path.read_bytes()
        dash = json.loads(raw)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return [Finding(path, "R000-json", f"invalid JSON: {exc}")]

    findings: list[Finding] = []

    def add(rule: str, msg: str) -> None:
        """Append a Finding for this dashboard."""
        findings.append(Finding(path, rule, msg))

    if len(raw) > _MAX_DASHBOARD_BYTES:
        add(
            "R008-size",
            f"dashboard is {len(raw)} bytes; maximum is {_MAX_DASHBOARD_BYTES}",
        )

    if not dash.get("title"):
        add("R001-title", "missing or empty .title")
    if not dash.get("uid"):
        add("R002-uid", "missing or empty .uid")

    sv = dash.get("schemaVersion")
    if not isinstance(sv, int) or sv < 38:
        add("R003-schema-version", f".schemaVersion must be >= 38, got {sv!r}")

    if dash.get("editable") is False:
        add("R004-editable", ".editable is false; manage read-only at the provider, not the JSON")

    for panel in _iter_panels(dash):
        if panel.get("type") == "row":
            continue
        if panel.get("type") == "text":
            continue
        if "datasource" not in panel or panel["datasource"] in (None, ""):
            add(
                "R005-panel-datasource",
                f"panel {panel.get('id', '?')} '{panel.get('title', '')}' has no datasource",
            )
        for tgt in panel.get("targets") or []:
            expr = tgt.get("expr") or ""
            if _SHORT_RATE.search(expr):
                add(
                    "R006-rate-interval",
                    f"panel {panel.get('id', '?')} target uses short fixed window: "
                    f"{expr!r} -- use [$__rate_interval]. Long deliberate windows "
                    "(>=10m, [Nh], [Nd]) are not flagged.",
                )

    templating = (dash.get("templating") or {}).get("list") or []
    if not any(v.get("type") == "datasource" for v in templating):
        add(
            "R007-templated-ds",
            "no templating variable of type 'datasource' (dashboard is not portable)",
        )

    for obj in _iter_objects(dash):
        datasource = obj.get("datasource")
        if isinstance(datasource, dict):
            uid = datasource.get("uid")
            if isinstance(uid, str) and uid != "-- Grafana --" and not uid.startswith("${"):
                add(
                    "R009-datasource-uid",
                    f"hard-coded datasource uid {uid!r}; use a datasource variable",
                )
        url = obj.get("url")
        if isinstance(url, str) and url and not _SUPPORTED_URL.match(url):
            add(
                "R010-url",
                f"unsupported dashboard URL {url!r}; use HTTPS or a relative Grafana URL",
            )

    return findings


@dataclass(frozen=True)
class LintResult:
    """Outcome of one lint run: the findings plus the pass/fail rule itself.

    `ok` is the rule -- a run passes iff nothing was found. Surfaces must
    read it rather than re-deriving `if findings:`, so a future rule (e.g.
    warn-level findings that don't fail) changes in exactly one place.
    """

    findings: tuple[Finding, ...]
    files_scanned: int

    @property
    def ok(self) -> bool:
        """True when the scanned dashboards produced no findings."""
        return not self.findings

    @property
    def files_with_findings(self) -> int:
        """How many distinct files contributed at least one finding."""
        return len({f.path for f in self.findings})


def expand_targets(paths: Iterable[Path]) -> list[Path]:
    """Resolve requested paths to dashboard files, descending into directories.

    A directory is the natural thing to hand `--path`, and until this existed
    it reached `Path.read_text` and killed the process with a raw
    `IsADirectoryError` traceback (design doc 8.9). Recursing is the reading
    that matches what the caller meant -- `--path charts/x/dashboards/` lints
    that tree -- and it makes `--path` and the default discovery agree, since
    `discover_dashboards` already rglobs.

    A directory containing no JSON contributes nothing rather than erroring,
    so it lands on the caller's existing "no dashboards found" decision (and
    its `--allow-empty` opt-out) instead of inventing a second rule for the
    same situation. Non-directories are passed through untouched, including
    ones that do not exist: "you named a file that is not there" is a
    distinct diagnostic and stays the caller's to report.
    """
    targets: list[Path] = []
    for p in paths:
        targets.extend(sorted(p.rglob("*.json")) if p.is_dir() else [p])
    return targets


def lint_paths(paths: Iterable[Path]) -> LintResult:
    """Lint every given dashboard file and aggregate the findings into a result."""
    targets = list(paths)
    findings: list[Finding] = []
    uids: dict[str, Path] = {}
    resource_names: dict[str, Path] = {}
    for p in targets:
        findings.extend(lint_dashboard(p))
        try:
            dashboard = json.loads(p.read_bytes())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        uid = dashboard.get("uid")
        if isinstance(uid, str) and uid:
            if first := uids.get(uid):
                findings.append(
                    Finding(p, "R011-duplicate-uid", f"uid {uid!r} is already used by {first}")
                )
            else:
                uids[uid] = p
        resource_name = rendered_configmap_name(p)
        if first := resource_names.get(resource_name):
            findings.append(
                Finding(
                    p,
                    "R012-rendered-name",
                    f"ConfigMap name {resource_name!r} is already produced by {first}",
                )
            )
        else:
            resource_names[resource_name] = p
    return LintResult(findings=tuple(findings), files_scanned=len(targets))


def discover_dashboards(
    root: Path,
    *,
    charts_dir: Path = DEFAULT_CHARTS_DIR,
) -> list[Path]:
    """Return all dashboard JSON files under the grafana-dashboards chart, sorted."""
    base = RepositoryLayout(
        root=root,
        charts_dir=charts_dir,
    ).chart_path("grafana-dashboards") / "dashboards"
    if not base.exists():
        return []
    return sorted(base.rglob("*.json"))
