"""Kubeconform integration for the validate schema phase.

Runs `kubeconform` over a directory of rendered manifests, parses the
JSON output, and surfaces a frozen report. Parse types live here (not in
plumbing/validate_models) because they're integration-local: the rest of
the pipeline consumes them via the schema phase, which collapses the
report into a PhaseResult.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError

_log = logging.getLogger(__name__)

ResourceStatus = Literal["valid", "invalid", "error", "skipped"]


@dataclass(frozen=True)
class ResourceResult:
    filename: str
    kind: str
    name: str
    status: ResourceStatus
    msg: str | None


@dataclass(frozen=True)
class KubeconformReport:
    resources: tuple[ResourceResult, ...]
    summary: Mapping[str, int]

    def invalid(self) -> tuple[ResourceResult, ...]:
        return tuple(r for r in self.resources if r.status in ("invalid", "error"))

    def has_failures(self) -> bool:
        return bool(self.invalid())


class Kubeconform:
    # datreeio CRDs catalog — covers most common in-tree CRDs. Charts that
    # vendor uncatalogued CRDs (cert-manager, istio-base) are addressed via
    # the CRD-skip default rather than a per-chart schema location.
    SCHEMA_LOCATION_CRDS = (
        "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
        "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
    )

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        binary: str | Path | None = None,
        timeout: float | None = None,
    ) -> None:
        self.runner = runner or CommandRunner()
        self._bin = str(binary) if binary is not None else "kubeconform"
        # Per-subprocess wall-clock cap. None = unbounded. Validate sets
        # this from --row-timeout so a hung kubeconform doesn't pin a worker.
        self.timeout = timeout

    def validate(
        self,
        manifests_dir: Path,
        *,
        kubernetes_version: str | None = None,
        schema_locations: list[str] | None = None,
        skip_kinds: list[str] | None = None,
        strict: bool = True,
        extra_args: list[str] | None = None,
    ) -> KubeconformReport:
        locations = (
            schema_locations
            if schema_locations is not None
            else ["default", self.SCHEMA_LOCATION_CRDS]
        )
        skips = skip_kinds if skip_kinds is not None else ["CustomResourceDefinition"]

        args: list[str] = [self._bin, "-output", "json", "-summary"]
        if strict:
            args.append("-strict")
        for loc in locations:
            args.extend(["-schema-location", loc])
        if skips:
            args.extend(["-skip", ",".join(skips)])
        if kubernetes_version is not None:
            args.extend(["-kubernetes-version", kubernetes_version])
        if extra_args:
            args.extend(extra_args)
        args.append(str(manifests_dir))

        result = self.runner.run(args, check=False, timeout=self.timeout)
        return _parse(result.stdout, args, result.returncode, result.stderr)


def _parse(
    stdout: str,
    args: list[str],
    returncode: int,
    stderr: str,
) -> KubeconformReport:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        # rc==0 with non-JSON would be a runtime contract violation; rc!=0 with
        # non-JSON is the typical tool crash. Treat both as ExternalCommandError
        # — the caller (phases.schema) tags it error_type="tool" -> exit 2.
        command = " ".join(args)
        detail = (stderr or stdout).strip()
        raise ExternalCommandError(
            f"kubeconform produced unparseable output ({returncode}): {command}\n"
            f"json error: {exc}\n{detail}"
        ) from exc

    resources_raw = data.get("resources") or []
    resources: list[ResourceResult] = []
    for entry in resources_raw:
        resources.append(
            ResourceResult(
                filename=entry.get("filename", ""),
                kind=entry.get("kind", ""),
                name=entry.get("name", ""),
                status=_normalize_status(entry.get("status", "")),
                msg=entry.get("msg") or None,
            )
        )
    summary = data.get("summary") or {}
    return KubeconformReport(resources=tuple(resources), summary=summary)


def _normalize_status(raw: str) -> ResourceStatus:
    # kubeconform emits statusValid/statusInvalid/statusError/statusSkipped/
    # statusEmpty. Unknown statuses get bucketed to "error" (never dropped)
    # and we log so a kubeconform version bump that adds a new status is
    # visible in CI/test output instead of silently misclassified.
    mapping: dict[str, ResourceStatus] = {
        "statusValid": "valid",
        "statusInvalid": "invalid",
        "statusError": "error",
        "statusSkipped": "skipped",
        "statusEmpty": "skipped",
    }
    normalized = mapping.get(raw)
    if normalized is None:
        _log.warning("kubeconform returned unknown status %r; bucketing as 'error'", raw)
        return "error"
    return normalized
