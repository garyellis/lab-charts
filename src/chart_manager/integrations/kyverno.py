"""Kyverno integration for the validate policy phase.

Runs `kyverno apply` over a directory of rendered manifests, parses the
JSON ClusterReport output, and surfaces a frozen report. Parse types live
here (not in plumbing/validate_models) — same convention as
`integrations/kubeconform.py:KubeconformReport`. Pipeline consumers go
through `phases.policy()`, which collapses the report into a PhaseResult.

Verified against kyverno CLI v1.18.1. The `apply --policy-report
--output-format json` envelope is an openreports.io/v1alpha1
ClusterReport with `summary` (pass/fail/warn/error/skip counts) and
`results[]` (one entry per (policy,rule,resource) triplet, each with a
single-element `resources` list in CLI mode). Regenerate fixtures when
bumping kyverno:

    helm template passing tests/fixtures/charts/passing-app --output-dir /tmp/r
    kyverno apply policies/ --resource /tmp/r/passing-app/templates/ \\
      --policy-report --output-format json > tests/fixtures/kyverno/pass.json
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError

_log = logging.getLogger(__name__)

PolicyStatus = Literal["pass", "fail", "warn", "skip", "error"]

# Conservative cap on the rendered exec argv length. Linux ARG_MAX is
# typically 2MB and macOS is 1MB; we cap well below the lower bound so
# the failure mode is a clear ValueError rather than an opaque
# `OSError: [Errno 7] Argument list too long` from execve(). If a chart
# legitimately needs more, the right answer is to switch to a single
# `--resource <dir>` plus a flat tmp tree (kyverno's non-recursive
# loader is the only reason we expand per-file today).
_MAX_ARGV_BYTES = 512 * 1024


@dataclass(frozen=True)
class PolicyResult:
    policy: str
    rule: str
    resource_kind: str
    resource_name: str
    resource_namespace: str | None
    status: PolicyStatus
    message: str | None


@dataclass(frozen=True)
class KyvernoReport:
    results: tuple[PolicyResult, ...]
    summary: Mapping[str, int]

    def failures(self) -> tuple[PolicyResult, ...]:
        return tuple(r for r in self.results if r.status in ("fail", "error"))

    def warnings(self) -> tuple[PolicyResult, ...]:
        return tuple(r for r in self.results if r.status == "warn")

    def has_failures(self) -> bool:
        return bool(self.failures())


class Kyverno:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        binary: str | Path | None = None,
        timeout: float | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self._bin = str(binary) if binary is not None else "kyverno"
        # Per-subprocess wall-clock cap. None = unbounded. Validate sets
        # this from --row-timeout so a hung kyverno doesn't pin a worker.
        self.timeout = timeout

    def apply(
        self,
        manifests_dir: Path,
        *,
        policy_paths: list[Path],
        extra_args: list[str] | None = None,
    ) -> KyvernoReport:
        # The phase function owns the "no policies discovered" SKIP decision.
        # Reaching here with an empty list is a programmer error, not a
        # runtime condition we should silently absorb.
        if not policy_paths:
            raise ValueError("policy_paths must be non-empty")

        # kyverno apply takes policies as positional args (path to file or
        # directory; repeated freely). `-p / --policy-report` switches
        # output to the openreports.io ClusterReport envelope;
        # `--output-format json` makes it parseable.
        #
        # NOTE on --resource: kyverno's CLI loader does NOT recurse into
        # subdirectories when --resource points at a directory — it only
        # reads files directly within it. Helm's `template --output-dir`
        # nests manifests at `<dir>/<chart>/templates/*.yaml`, so passing
        # the parent would silently match zero resources. We walk the
        # tree ourselves and pass each manifest as an individual
        # --resource flag. kyverno accepts the flag repeatedly.
        manifests = _discover_manifests(manifests_dir)
        if not manifests:
            # No manifests => identical to the empty-stdout case below.
            return KyvernoReport(results=(), summary={})

        args: list[str] = [self._bin, "apply"]
        args.extend(str(p) for p in policy_paths)
        for manifest in manifests:
            args.extend(["--resource", str(manifest)])
        args.extend(["--policy-report", "--output-format", "json"])
        if extra_args:
            args.extend(extra_args)

        # Pre-flight the argv length so very large charts (deps-installed
        # prometheus-operator etc.) fail with a clear message instead of an
        # opaque execve E2BIG. See _MAX_ARGV_BYTES comment for the cap rationale.
        argv_bytes = sum(len(a.encode()) for a in args) + len(args)
        if argv_bytes > _MAX_ARGV_BYTES:
            raise ValueError(
                f"kyverno argv exceeds {_MAX_ARGV_BYTES} bytes "
                f"({argv_bytes} bytes, {len(manifests)} manifests). "
                "Render a smaller subtree or pre-flatten manifests into a single dir."
            )

        result = self.runner.run(args, check=False, timeout=self.timeout)
        return _parse(result.stdout, args, result.returncode, result.stderr)


def _discover_manifests(root: Path) -> list[Path]:
    """Walk `root` and return every .yaml/.yml regular file.

    `followlinks=False` matches the schema phase's `_has_manifests` policy
    so a cyclic symlink in the rendered tree can't hang us.
    """
    found: list[Path] = []
    if not root.exists():
        return found
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            if name.endswith((".yaml", ".yml")):
                full = Path(dirpath) / name
                if full.is_file() and not full.is_symlink():
                    found.append(full)
    return found


def _parse(
    stdout: str,
    args: list[str],
    returncode: int,
    stderr: str,
) -> KyvernoReport:
    # kyverno exits 0 with empty stdout when --resource targets a directory
    # that contains no kyverno-recognized resources. Treat that as an empty
    # report rather than a parse failure; the phase function decides whether
    # that's PASS or SKIP based on the rendered-dir contents.
    stripped = stdout.strip()
    if not stripped:
        return KyvernoReport(results=(), summary={})

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        command = " ".join(args)
        detail = (stderr or stdout).strip()
        raise ExternalCommandError(
            f"kyverno produced unparseable output ({returncode}): {command}\n"
            f"json error: {exc}\n{detail}"
        ) from exc

    results_raw = data.get("results") or []
    results: list[PolicyResult] = []
    for entry in results_raw:
        # CLI mode emits one resource per result entry, but the field is
        # always a list. Defensive: emit one PolicyResult per resource so
        # a future kyverno that bundles wouldn't drop findings silently.
        resources = entry.get("resources") or [{}]
        for res in resources:
            results.append(
                PolicyResult(
                    policy=entry.get("policy", ""),
                    rule=entry.get("rule", ""),
                    resource_kind=res.get("kind", ""),
                    resource_name=res.get("name", ""),
                    resource_namespace=res.get("namespace") or None,
                    status=_normalize_status(entry.get("result", "")),
                    message=entry.get("message") or None,
                )
            )

    summary = data.get("summary") or {}
    # Coerce to plain dict[str,int] — drops the timestamp/nanos sub-objects
    # the envelope sometimes carries while keeping the counters intact.
    summary_clean = {k: int(v) for k, v in summary.items() if isinstance(v, int)}
    return KyvernoReport(results=tuple(results), summary=summary_clean)


def _normalize_status(raw: str) -> PolicyStatus:
    # kyverno result values are pass/fail/warn/error/skip. Anything else
    # gets bucketed to "error" and logged so an upstream output-format
    # change is visible rather than silently misclassified — mirrors the
    # Kubeconform._normalize_status discipline.
    valid: tuple[PolicyStatus, ...] = ("pass", "fail", "warn", "skip", "error")
    if raw in valid:
        return raw
    _log.warning("kyverno returned unknown result %r; bucketing as 'error'", raw)
    return "error"
