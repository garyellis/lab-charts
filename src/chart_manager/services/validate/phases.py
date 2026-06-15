"""Pure phase functions.

Each phase takes the inputs it needs and returns a PhaseResult. No phase
opens the filesystem outside its declared inputs; no phase decides whether
to short-circuit downstream phases (the runner owns that).
"""
from __future__ import annotations

import os
from pathlib import Path

from chart_manager.integrations.helm import Helm
from chart_manager.integrations.kubeconform import Kubeconform, ResourceResult
from chart_manager.integrations.kyverno import Kyverno, PolicyResult
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.validate_models import PhaseResult, WorklistRow


def render(
    row: WorklistRow,
    *,
    helm: Helm,
    chart_path: Path,
    values: list[Path],
    output_root: Path,
) -> PhaseResult:
    """Render one (chart, env) into output_root/<chart>/<env>/.

    Output layout matches the worklist row keys so downstream phases
    (schema/policy in M2/M3) can locate manifests by row identity.
    """
    out_dir = (output_root / row.chart / row.env).resolve()
    try:
        rendered = helm.template(
            row.release,
            chart_path,
            namespace=row.namespace,
            output_dir=out_dir,
            values=values,
        )
    except ExternalCommandError as exc:
        # error_type="tool" promotes the row to exit code 2 — the underlying
        # issue is a helm crash, not a chart-author validation problem.
        return PhaseResult(
            phase="render",
            status="FAIL",
            detail=str(exc),
            artifacts=(),
            error_type="tool",
        )

    return PhaseResult(
        phase="render",
        status="PASS",
        detail=None,
        artifacts=(rendered,),
    )


def schema(
    row: WorklistRow,
    *,
    kubeconform: Kubeconform,
    rendered_dir: Path,
    kubernetes_version: str | None = None,
    schema_locations: list[str] | None = None,
) -> PhaseResult:
    """Run kubeconform over rendered manifests for one row.

    Empty rendered_dir -> SKIP. Tool crash -> FAIL with error_type="tool"
    (exit code 2). Schema violations -> FAIL with a human-scannable
    one-line-per-finding detail block.
    """
    if not rendered_dir.exists() or not _has_manifests(rendered_dir):
        return PhaseResult(phase="schema", status="SKIP", detail="no manifests")

    try:
        report = kubeconform.validate(
            rendered_dir,
            kubernetes_version=kubernetes_version,
            schema_locations=schema_locations,
        )
    except ExternalCommandError as exc:
        return PhaseResult(
            phase="schema",
            status="FAIL",
            detail=str(exc),
            error_type="tool",
        )

    if not report.has_failures():
        return PhaseResult(phase="schema", status="PASS")

    return PhaseResult(
        phase="schema",
        status="FAIL",
        detail=_format_findings(report.invalid()),
    )


def policy(
    row: WorklistRow,
    *,
    kyverno: Kyverno,
    rendered_dir: Path,
    policy_paths: list[Path],
) -> PhaseResult:
    """Run kyverno over the rendered manifests for one row.

    Empty policy_paths -> SKIP("no policies discovered") so charts without
    any policy coverage surface visibly (run summary later tallies these).
    Empty rendered_dir -> SKIP("no manifests"). Tool crash -> FAIL with
    error_type="tool" (exit code 2). Policy violations -> FAIL with one
    line per finding.
    """
    if not policy_paths:
        return PhaseResult(phase="policy", status="SKIP", detail="no policies discovered")

    if not rendered_dir.exists() or not _has_manifests(rendered_dir):
        return PhaseResult(phase="policy", status="SKIP", detail="no manifests")

    try:
        report = kyverno.apply(rendered_dir, policy_paths=policy_paths)
    except ExternalCommandError as exc:
        return PhaseResult(
            phase="policy",
            status="FAIL",
            detail=str(exc),
            error_type="tool",
        )

    warns = report.warnings()
    if not report.has_failures():
        # Surface warns as an advisory on a PASS row (no exit-code change);
        # rendering picks them up via the non-empty detail on PASS phases.
        if warns:
            return PhaseResult(
                phase="policy",
                status="PASS",
                detail="warnings:\n" + _format_policy_findings(warns),
            )
        return PhaseResult(phase="policy", status="PASS")

    detail = _format_policy_findings(report.failures())
    if warns:
        detail += "\n\nwarnings:\n" + _format_policy_findings(warns)
    return PhaseResult(
        phase="policy",
        status="FAIL",
        detail=detail,
    )


def _format_policy_findings(findings: tuple[PolicyResult, ...]) -> str:
    lines: list[str] = []
    for f in findings:
        msg = f.message or ""
        lines.append(
            f"{f.policy}/{f.rule}: {f.resource_kind}/{f.resource_name}: {msg}".rstrip(": ")
        )
    return "\n".join(lines)


def _has_manifests(path: Path) -> bool:
    # os.walk with followlinks=False avoids infinite recursion on cyclic
    # symlinks. Path.rglob follows symlinked directories by default, which
    # is unsafe against a rendered tree that could contain user-controlled
    # symlinks (helm doesn't emit them today, but the guarantee is cheap).
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            if name.endswith((".yaml", ".yml")):
                # Confirm it's a regular file (not a symlink to one) so we
                # don't count dangling/looping symlink targets.
                full = Path(dirpath) / name
                if full.is_file() and not full.is_symlink():
                    return True
    return False


def _format_findings(resources: tuple[ResourceResult, ...]) -> str:
    lines: list[str] = []
    for r in resources:
        msg = r.msg or ""
        lines.append(f"{r.kind}/{r.name} ({r.filename}): {msg}".rstrip(": "))
    return "\n".join(lines)
