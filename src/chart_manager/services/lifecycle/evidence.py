"""Durable evidence produced while executing lifecycle actions.

Evidence is deliberately separate from the authored chart configuration.  The
records in this module are immutable observations of a particular action and
input digest; callers may use them as a local cache, but should not confuse a
cached observation with live cluster state.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast, get_args
from uuid import uuid4

EVIDENCE_API_VERSION = "lifecycle.cmg.io/evidence/v1alpha1"
EvidenceSource = Literal["local", "ci", "live"]
EvidenceVerdict = Literal["PASS", "FAIL", "SKIP", "UNKNOWN"]
EvidenceStatus = Literal[
    "PASS",
    "FAIL",
    "SKIP",
    "UNKNOWN",
    "deployed",
    "failed",
    "uninstalled",
    "superseded",
    "uninstalling",
    "pending-install",
    "pending-upgrade",
    "pending-rollback",
    "unknown",
    "ready",
    "not-ready",
    "missing",
    "unobservable",
]

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_VERDICTS = frozenset(get_args(EvidenceVerdict))
_STATUSES = frozenset(get_args(EvidenceStatus))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _datetime_to_json(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("evidence timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _datetime_from_json(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class TargetCoordinates:
    """Coordinates identifying the unit an action operated on."""

    chart: str
    workflow: str
    profile: str | None = None
    environment: str | None = None
    release: str | None = None
    namespace: str | None = None


@dataclass(frozen=True)
class ClusterIdentity:
    """Identity captured during a cluster-backed action, when applicable."""

    name: str
    context: str | None = None
    uid: str | None = None
    kubernetes_version: str | None = None


@dataclass(frozen=True)
class EvidenceRecord:
    """One versioned, JSON-serializable lifecycle action observation."""

    run_id: str
    action_id: str
    action_kind: str
    target: TargetCoordinates
    verdict: EvidenceVerdict
    status: EvidenceStatus
    input_digest: str
    started_at: datetime
    finished_at: datetime
    source: EvidenceSource = "local"
    reason: str | None = None
    detail: str | None = None
    artifacts: tuple[str, ...] = ()
    toolchain: Mapping[str, str] = field(default_factory=dict)
    cluster: ClusterIdentity | None = None
    recorded_at: datetime = field(default_factory=_utc_now)
    evidence_id: str = field(default_factory=lambda: str(uuid4()))
    api_version: str = EVIDENCE_API_VERSION

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.run_id):
            raise ValueError(
                "run_id must contain only letters, numbers, '.', '_', or '-' "
                "and be at most 128 characters"
            )
        # Action IDs are logical compiler identities, never filenames. Colons
        # are part of the compiler's stable vocabulary
        # (``validation:grafana:dev:render``).
        if not _ACTION_ID.fullmatch(self.action_id):
            raise ValueError(
                "action_id must contain only letters, numbers, '.', '_', '-', or ':' "
                "and be at most 256 characters"
            )
        if not _SAFE_ID.fullmatch(self.evidence_id):
            raise ValueError("evidence_id is not path-safe")
        if self.api_version != EVIDENCE_API_VERSION:
            raise ValueError(f"unsupported evidence apiVersion: {self.api_version!r}")
        if self.source not in {"local", "ci", "live"}:
            raise ValueError(f"unsupported evidence source: {self.source!r}")
        if self.verdict not in _VERDICTS:
            raise ValueError(f"unsupported evidence verdict: {self.verdict!r}")
        if self.status not in _STATUSES:
            raise ValueError(f"unsupported evidence status: {self.status!r}")
        if not self.input_digest:
            raise ValueError("input_digest must not be empty")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        for timestamp in (self.started_at, self.finished_at, self.recorded_at):
            _datetime_to_json(timestamp)

    @property
    def elapsed_seconds(self) -> float:
        """Return action wall-clock duration in seconds."""

        return (self.finished_at - self.started_at).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the stable on-disk JSON shape."""

        data = asdict(self)
        data["apiVersion"] = data.pop("api_version")
        data["startedAt"] = _datetime_to_json(data.pop("started_at"))
        data["finishedAt"] = _datetime_to_json(data.pop("finished_at"))
        data["recordedAt"] = _datetime_to_json(data.pop("recorded_at"))
        data["elapsedSeconds"] = self.elapsed_seconds
        data["runId"] = data.pop("run_id")
        data["actionId"] = data.pop("action_id")
        data["actionKind"] = data.pop("action_kind")
        data["inputDigest"] = data.pop("input_digest")
        data["evidenceId"] = data.pop("evidence_id")
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvidenceRecord:
        """Parse and validate one persisted evidence record."""

        if data.get("apiVersion") != EVIDENCE_API_VERSION:
            raise ValueError(f"unsupported evidence apiVersion: {data.get('apiVersion')!r}")
        target_raw = data.get("target")
        if not isinstance(target_raw, Mapping):
            raise ValueError("target must be an object")
        cluster_raw = data.get("cluster")
        if cluster_raw is not None and not isinstance(cluster_raw, Mapping):
            raise ValueError("cluster must be an object or null")
        toolchain = data.get("toolchain", {})
        if not isinstance(toolchain, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in toolchain.items()
        ):
            raise ValueError("toolchain must be an object containing string values")
        artifacts = data.get("artifacts", ())
        if not isinstance(artifacts, (list, tuple)) or not all(
            isinstance(item, str) for item in artifacts
        ):
            raise ValueError("artifacts must be an array of strings")

        try:
            target = TargetCoordinates(**dict(target_raw))
            cluster = ClusterIdentity(**dict(cluster_raw)) if cluster_raw is not None else None
            return cls(
                api_version=str(data["apiVersion"]),
                evidence_id=str(data["evidenceId"]),
                run_id=str(data["runId"]),
                action_id=str(data["actionId"]),
                action_kind=str(data["actionKind"]),
                target=target,
                verdict=cast(EvidenceVerdict, str(data["verdict"])),
                status=cast(EvidenceStatus, str(data["status"])),
                reason=None if data.get("reason") is None else str(data["reason"]),
                detail=None if data.get("detail") is None else str(data["detail"]),
                artifacts=tuple(artifacts),
                input_digest=str(data["inputDigest"]),
                toolchain=dict(toolchain),
                cluster=cluster,
                source=cast(EvidenceSource, str(data.get("source", "local"))),
                started_at=_datetime_from_json(data.get("startedAt"), "startedAt"),
                finished_at=_datetime_from_json(data.get("finishedAt"), "finishedAt"),
                recorded_at=_datetime_from_json(data.get("recordedAt"), "recordedAt"),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"invalid evidence record: {exc}") from exc


@dataclass(frozen=True)
class EvidenceDiagnostic:
    """A corrupt or unreadable record encountered during repository reads."""

    path: Path
    message: str


@dataclass(frozen=True)
class EvidenceHistory:
    """Records plus non-fatal diagnostics from a repository read."""

    records: tuple[EvidenceRecord, ...]
    diagnostics: tuple[EvidenceDiagnostic, ...] = ()


@dataclass(frozen=True)
class LatestEvidence:
    """Latest matching record plus non-fatal repository diagnostics."""

    record: EvidenceRecord | None
    diagnostics: tuple[EvidenceDiagnostic, ...] = ()


class LocalEvidenceRepository:
    """Append-only atomic JSON repository rooted at a caller-selected path."""

    def __init__(self, root: Path) -> None:
        lexical_root = Path(os.path.abspath(root))
        resolved_root = root.resolve()
        if lexical_root != resolved_root:
            raise ValueError("evidence repository root must not contain symlinks")
        self.root = lexical_root
        self.evidence_dir = self.root / "evidence"
        self.records_dir = self.root / "evidence" / EVIDENCE_API_VERSION.rsplit("/", 1)[-1]

    def _path_is_safe(self, path: Path) -> bool:
        """Whether ``path`` is lexically and physically beneath the fixed root."""

        lexical = Path(os.path.abspath(path))
        try:
            lexical.relative_to(self.root)
        except ValueError:
            return False
        return lexical.resolve() == lexical

    def _ensure_directory(self, path: Path) -> None:
        """Create one directory only after checking all existing ancestors."""

        if not self._path_is_safe(path):
            raise ValueError(f"evidence path contains a symlink or escapes its root: {path}")
        path.mkdir(exist_ok=True)
        if path.is_symlink() or not self._path_is_safe(path):
            raise ValueError(f"evidence path contains a symlink or escapes its root: {path}")

    def append(self, record: EvidenceRecord) -> Path:
        """Atomically append a record and return its final path."""

        if not self._path_is_safe(self.root):
            raise ValueError("evidence repository root contains a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self._path_is_safe(self.root):
            raise ValueError("evidence repository root contains a symlink")
        self._ensure_directory(self.evidence_dir)
        self._ensure_directory(self.records_dir)
        destination = self.records_dir / f"{record.evidence_id}.json"
        if not self._path_is_safe(destination):
            raise ValueError("evidence record path contains a symlink or escapes its root")
        if destination.exists():
            raise FileExistsError(f"evidence record already exists: {record.evidence_id}")
        payload = json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n"
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{record.evidence_id}.", suffix=".tmp", dir=self.records_dir
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination

    def history(
        self,
        *,
        action_id: str | None = None,
        target: TargetCoordinates | None = None,
        input_digest: str | None = None,
    ) -> EvidenceHistory:
        """Read matching records while reporting corrupt records explicitly."""

        if (
            not self._path_is_safe(self.root)
            or not self._path_is_safe(self.evidence_dir)
            or not self._path_is_safe(self.records_dir)
        ):
            return EvidenceHistory(
                records=(),
                diagnostics=(
                    EvidenceDiagnostic(
                        self.records_dir,
                        "evidence path contains a symlink or escapes repository root",
                    ),
                ),
            )
        if not self.records_dir.exists():
            return EvidenceHistory(records=())
        records: list[EvidenceRecord] = []
        diagnostics: list[EvidenceDiagnostic] = []
        for path in sorted(self.records_dir.glob("*.json")):
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                diagnostics.append(EvidenceDiagnostic(path, "record path escapes repository root"))
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping):
                    raise ValueError("top-level JSON value must be an object")
                record = EvidenceRecord.from_dict(raw)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                diagnostics.append(EvidenceDiagnostic(path, str(exc)))
                continue
            if action_id is not None and record.action_id != action_id:
                continue
            if target is not None and record.target != target:
                continue
            if input_digest is not None and record.input_digest != input_digest:
                continue
            records.append(record)
        records.sort(key=lambda item: (item.recorded_at, item.evidence_id))
        return EvidenceHistory(tuple(records), tuple(diagnostics))

    def latest(
        self,
        *,
        action_id: str,
        target: TargetCoordinates | None = None,
        input_digest: str | None = None,
    ) -> LatestEvidence:
        """Return the latest matching evidence and all read diagnostics."""

        history = self.history(
            action_id=action_id,
            target=target,
            input_digest=input_digest,
        )
        record = history.records[-1] if history.records else None
        return LatestEvidence(record, history.diagnostics)
