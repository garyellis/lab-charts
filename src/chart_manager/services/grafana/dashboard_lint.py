"""Lint Grafana dashboard JSON files for repo-wide quality rules.

Pure-Python, stdlib-only. Designed to be fast and to produce stable,
greppable output suitable for CI.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def _iter_panels(dash: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Yield every panel, descending into row panels' nested `panels`."""
    def walk(panels: Any) -> Iterable[dict[str, Any]]:
        """Recurse one panel list, recursing again into row containers."""
        for p in panels or []:
            yield p
            if p.get("type") == "row" and p.get("panels"):
                yield from walk(p["panels"])

    yield from walk(dash.get("panels"))


def lint_dashboard(path: Path) -> list[Finding]:
    """Lint one dashboard JSON file against rules R001-R007; invalid JSON is R000."""
    try:
        dash = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [Finding(path, "R000-json", f"invalid JSON: {exc}")]

    findings: list[Finding] = []

    def add(rule: str, msg: str) -> None:
        """Append a Finding for this dashboard."""
        findings.append(Finding(path, rule, msg))

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


def lint_paths(paths: Iterable[Path]) -> LintResult:
    """Lint every given dashboard file and aggregate the findings into a result."""
    targets = list(paths)
    findings: list[Finding] = []
    for p in targets:
        findings.extend(lint_dashboard(p))
    return LintResult(findings=tuple(findings), files_scanned=len(targets))


def discover_dashboards(root: Path) -> list[Path]:
    """Return all dashboard JSON files under the grafana-dashboards chart, sorted."""
    base = root / "charts" / "grafana-dashboards" / "dashboards"
    if not base.exists():
        return []
    return sorted(base.rglob("*.json"))
