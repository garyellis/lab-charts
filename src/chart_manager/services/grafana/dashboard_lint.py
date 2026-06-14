"""Lint Grafana dashboard JSON files for repo-wide quality rules.

Pure-Python, stdlib-only. Designed to be fast and to produce stable,
greppable output suitable for CI.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class Finding:
    path: Path
    rule: str
    message: str

    def render(self) -> str:
        return f"{self.path}: [{self.rule}] {self.message}"


_SHORT_RATE = re.compile(
    r"\b(rate|irate|increase)\s*\([^)]*\[(?:\d+s|[1-5]m)\]"
)


def _iter_panels(dash: dict) -> Iterable[dict]:
    def walk(panels):
        for p in panels or []:
            yield p
            if p.get("type") == "row" and p.get("panels"):
                yield from walk(p["panels"])

    yield from walk(dash.get("panels"))


def lint_dashboard(path: Path) -> list[Finding]:
    try:
        dash = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [Finding(path, "R000-json", f"invalid JSON: {exc}")]

    findings: list[Finding] = []

    def add(rule: str, msg: str) -> None:
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


def lint_paths(paths: Iterable[Path]) -> list[Finding]:
    out: list[Finding] = []
    for p in paths:
        out.extend(lint_dashboard(p))
    return out


def discover_dashboards(root: Path) -> list[Path]:
    base = root / "charts" / "grafana-dashboards" / "dashboards"
    if not base.exists():
        return []
    return sorted(base.rglob("*.json"))
