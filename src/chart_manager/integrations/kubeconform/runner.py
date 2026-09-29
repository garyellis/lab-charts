"""Kubeconform integration for the validate schema phase.

Runs `kubeconform` over a directory of rendered manifests, parses the
JSON output, and surfaces a frozen report. Parse types live here (not in
services/manifest_validation/models) because they're integration-local: the rest of
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
from urllib.parse import urlsplit

from chart_manager.plumbing.commands import CommandRunner, SubprocessRunner
from chart_manager.plumbing.errors import ExternalCommandError
from chart_manager.plumbing.preflight import Check, probe_binary
from chart_manager.plumbing.yaml_files import load_yaml_documents

_log = logging.getLogger(__name__)

ResourceStatus = Literal["valid", "invalid", "error", "skipped"]


@dataclass(frozen=True)
class ResourceResult:
    """One resource's validation verdict from kubeconform's JSON output."""

    filename: str
    kind: str
    name: str
    status: ResourceStatus
    msg: str | None


@dataclass(frozen=True)
class KubeconformReport:
    """Full kubeconform run: per-resource results plus the summary counters."""

    resources: tuple[ResourceResult, ...]
    summary: Mapping[str, int]

    def invalid(self) -> tuple[ResourceResult, ...]:
        """Resources with status invalid or error."""
        return tuple(r for r in self.resources if r.status in ("invalid", "error"))

    def errors(self) -> tuple[ResourceResult, ...]:
        """Tool/schema-resolution errors, distinct from invalid resources."""
        return tuple(r for r in self.resources if r.status == "error")

    def has_failures(self) -> bool:
        """True if any resource failed validation."""
        return bool(self.invalid())


class Kubeconform:
    """Run `kubeconform` over rendered manifests and parse its JSON report."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        binary: str | Path | None = None,
        timeout: float | None = None,
    ) -> None:
        """Bind a CommandRunner, binary path (default `kubeconform`), and timeout."""
        self.runner = runner or SubprocessRunner()
        self._bin = str(binary) if binary is not None else "kubeconform"
        # Per-subprocess wall-clock cap. None = unbounded. Validate sets
        # this from --tool-timeout so a hung kubeconform doesn't pin a worker.
        self.timeout = timeout

    def preflight(self) -> tuple[Check, ...]:
        """Report whether the configured kubeconform binary is usable.

        `-v` rather than `--version`: kubeconform accepts only the short
        spelling, and the long one exits non-zero -- which would report a
        perfectly good install as broken.
        """
        return (
            probe_binary(
                self.runner,
                self._bin,
                name="kubeconform",
                version_args=("-v",),
                remediation=(
                    "install kubeconform -- https://github.com/yannh/kubeconform#installation"
                ),
            ),
        )

    def validate(
        self,
        manifests_dir: Path,
        *,
        kubernetes_version: str | None = None,
        schema_locations: list[str],
        skip_kinds: list[str] | None = None,
        strict: bool = True,
        extra_args: list[str] | None = None,
    ) -> KubeconformReport:
        """Validate all manifests under `manifests_dir`; return the parsed report.

        Schema inputs must be explicit local paths supplied by the managed
        store and chart-local additions. No remote/default fallback and no
        resource kind are skipped implicitly. Validation failures land in the
        report (``check=False``); invalid configuration or output raises.
        """
        if not schema_locations:
            raise ExternalCommandError(
                "kubeconform requires at least one explicit local schema location"
            )
        for location in schema_locations:
            parsed = urlsplit(location)
            if not location.strip() or location == "default" or parsed.scheme or parsed.netloc:
                raise ExternalCommandError(
                    f"kubeconform schema locations must be local paths, got {location!r}"
                )
        skips = _uncovered_gvk_skips(
            manifests_dir,
            schema_locations,
            frozenset({*(skip_kinds or []), "CustomResourceDefinition"}),
        )

        args: list[str] = [self._bin, "-output", "json", "-summary"]
        if strict:
            args.append("-strict")
        for loc in schema_locations:
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
    """Parse kubeconform JSON stdout into a report; raise on unparseable output."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        # rc==0 with non-JSON would be a runtime contract violation; rc!=0 with
        # non-JSON is the typical tool crash. Treat both as ExternalCommandError
        # — the caller tags it error_type="tool" -> TOOL (exit 4).
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
    """Map kubeconform status strings to our ResourceStatus; unknowns become 'error'."""
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


def _uncovered_gvk_skips(
    manifests_dir: Path,
    schema_locations: list[str],
    allowed_kinds: frozenset[str],
) -> list[str]:
    """Expand authored Kind exceptions to only uncovered concrete GVKs."""
    skips: set[str] = set()
    for path in sorted(manifests_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".yaml", ".yml"}:
            continue
        try:
            documents = load_yaml_documents(path)
        except Exception:
            # Kubeconform owns malformed-manifest diagnostics. Preprocessing
            # must never hide or reclassify those findings.
            continue
        for raw in documents:
            candidates = (
                raw.get("items", ())
                if isinstance(raw, dict) and raw.get("kind") == "List"
                else (raw,)
            )
            if not isinstance(candidates, (list, tuple)):
                continue
            for document in candidates:
                if not isinstance(document, dict):
                    continue
                api_version = document.get("apiVersion")
                kind = document.get("kind")
                if not isinstance(api_version, str) or not isinstance(kind, str):
                    continue
                if kind not in allowed_kinds and f"{api_version}/{kind}" not in allowed_kinds:
                    continue
                group, _, version = api_version.rpartition("/")
                if not group:
                    version = api_version
                template_group = group or version
                replacements = {
                    "{{.Group}}": template_group,
                    "{{.ResourceKind}}": kind.lower(),
                    "{{.ResourceAPIVersion}}": version,
                }
                covered = False
                for location in schema_locations:
                    expanded = location
                    for marker, value in replacements.items():
                        expanded = expanded.replace(marker, value)
                    if Path(expanded).is_file():
                        covered = True
                        break
                if not covered:
                    skips.add(f"{api_version}/{kind}")
    return sorted(skips)


__all__ = [
    "Kubeconform",
    "KubeconformReport",
    "ResourceResult",
    "ResourceStatus",
]
