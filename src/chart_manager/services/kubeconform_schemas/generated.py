"""Derived CRD schemas cached independently of upstream repository pins."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from chart_manager.domain.chart_deps import deps_are_fresh
from chart_manager.domain.workspace import RepositoryWorkspace
from chart_manager.services.kubeconform_schemas.crd import generate_crd_schemas
from chart_manager.services.kubeconform_schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaRenderError,
    KubeconformSchemaStoreError,
)
from chart_manager.services.kubeconform_schemas.inventory import scan_rendered_directory
from chart_manager.services.kubeconform_schemas.models import (
    GroupVersionKind,
    MaterializedSchema,
    SchemaScope,
    content_digest,
)
from chart_manager.services.kubeconform_schemas.store import default_schema_cache_root
from chart_manager.services.manifest_validation.catalog import build_catalog
from chart_manager.services.manifest_validation.models import ManifestValidationTarget, RunRequest

if TYPE_CHECKING:
    from chart_manager.services.manifest_validation.app import ManifestValidationService


_LOG = logging.getLogger(__name__)


class _Fingerprints:
    """Share tool hashes within one preparation; recheck chart bytes each time."""

    def __init__(self) -> None:
        self.binaries: dict[tuple[str, int, int, int], bytes] = {}
        self.implementation: bytes | None = None
        root = Path(__file__).resolve().parents[2]
        digest = hashlib.sha256()
        try:
            for path in sorted(root.rglob("*.py")):
                if path.is_symlink():
                    return
                digest.update(str(path.relative_to(root)).encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
            self.implementation = digest.digest()
        except OSError:
            pass

    def chart(self, target: ManifestValidationTarget) -> str | None:
        dependencies = target.chart.metadata.dependencies
        if self.implementation is None or target.spec.helm_version:
            return None
        if dependencies and (
            not deps_are_fresh(target.path)
            or any((dependency.repository or "").startswith("file:") for dependency in dependencies)
        ):
            return None
        binary = shutil.which(target.spec.helm_binary or "helm")
        if binary is None:
            return None
        digest = hashlib.sha256(b"derived-crd-cache-v2")
        digest.update(self.implementation)
        try:
            stat = Path(binary).stat()
            key = (binary, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if key not in self.binaries:
                self.binaries[key] = hashlib.sha256(Path(binary).read_bytes()).digest()
            digest.update(self.binaries[key])
            for path in sorted(target.path.rglob("*")):
                if path.is_symlink():
                    return None
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                digest.update(str(path.relative_to(target.path)).encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        except OSError:
            return None
        return digest.hexdigest()


def _load_cached(path: Path) -> tuple[MaterializedSchema, ...] | None:
    try:
        wrapper = json.loads(path.read_bytes())
        payload = wrapper["payload"]
        if content_digest(payload.encode()) != wrapper["checksum"]:
            return None
        return tuple(
            MaterializedSchema(
                gvk=GroupVersionKind.model_validate(item["gvk"]),
                source="generated",
                scope=None,
                content=item["content"].encode(),
                source_reference=item["source"],
            )
            for item in json.loads(payload)
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        KubeconformSchemaConfigurationError,
    ):
        return None


def _write_cached(path: Path, items: tuple[MaterializedSchema, ...]) -> None:
    payload = json.dumps(
        [
            {
                "gvk": item.gvk.model_dump(),
                "content": item.content.decode(),
                "source": item.source_reference,
            }
            for item in items
        ],
        sort_keys=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".staging-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump({"checksum": content_digest(payload.encode()), "payload": payload}, stream)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def prepare_generated_schemas(
    workspace: RepositoryWorkspace,
    validation: ManifestValidationService,
    *,
    cache_root: Path | None = None,
) -> tuple[str, ...]:
    if workspace.validation is None or not workspace.validation.schemas.generate_from_crds:
        return ()
    root = (cache_root or default_schema_cache_root()) / "v3" / "derived"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".prepare.lock").open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                return _prepare_generated_schemas(workspace, validation, cache_root=cache_root)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise KubeconformSchemaStoreError(f"cannot prepare derived schemas: {exc}") from exc


def _prepare_generated_schemas(
    workspace: RepositoryWorkspace,
    validation: ManifestValidationService,
    *,
    cache_root: Path | None = None,
) -> tuple[str, ...]:
    """Automatically refresh changed chart providers before loading generated schemas.

    Repository-wide sharing is preserved. Unchanged charts reuse their derived
    results; a new chart or modified CRD is rendered without a schema-sync step.
    These files are disposable build outputs and never enter schemas.lock.yaml.
    """
    if workspace.validation is None or not workspace.validation.schemas.generate_from_crds:
        return ()
    catalog = build_catalog(workspace.root, charts_dir=workspace.charts_dir)
    if catalog.errors:
        raise KubeconformSchemaConfigurationError(
            "cannot discover CRD providers: " + "; ".join(catalog.errors)
        )
    root = (cache_root or default_schema_cache_root()) / "v3" / "derived"
    by_gvk: dict[str, tuple[str, MaterializedSchema]] = {}
    fingerprints = _Fingerprints()
    prepared = []
    for target in catalog.targets:
        fingerprint = fingerprints.chart(target)
        cache = root / "charts" / f"{fingerprint}.json" if fingerprint else None
        schemas = _load_cached(cache) if cache is not None else None
        prepared.append((target, fingerprint, cache, schemas))
    cached = sum(schemas is not None for _, _, _, schemas in prepared)
    _LOG.info(
        "Preparing CRD schemas: %d charts cached, %d to render",
        cached, len(prepared) - cached,
    )
    for target, fingerprint, cache, schemas in prepared:
        if schemas is None:
            with tempfile.TemporaryDirectory(prefix="chart-manager-crds-") as temporary:
                output = Path(temporary)
                outcome = validation.run(
                    RunRequest(
                        root=workspace.root,
                        charts=(target.name,),
                        phases=frozenset({"render"}),
                        out=output,
                        keep=True,
                        include_crds=True,
                    )
                )
                failures = [
                    f"{row.row.chart}/{row.row.env}: {row.phases['render'].detail}"
                    for row in outcome.result.rows
                    if row.phases["render"].status != "PASS"
                ]
                if failures or outcome.result.spec_errors:
                    raise KubeconformSchemaRenderError(
                        "CRD provider render failed:\n"
                        + "\n".join((*failures, *outcome.result.spec_errors)),
                        outcome=outcome.result.outcome(),
                    )
                crds = [
                    crd
                    for row in outcome.result.rows
                    for crd in scan_rendered_directory(
                        output / row.row.chart / row.row.env,
                        scope=SchemaScope(chart=row.row.chart, environment=row.row.env),
                    ).crds
                ]
                schemas = generate_crd_schemas(crds)
            # Don't publish under an input hash if files changed during rendering.
            if cache is not None and fingerprint == fingerprints.chart(target):
                _write_cached(cache, schemas)
        for schema in schemas:
            previous = by_gvk.get(schema.gvk.key)
            if previous and previous[1].content != schema.content:
                raise KubeconformSchemaConfigurationError(
                    f"conflicting rendered CRDs define {schema.gvk.key}: "
                    f"{previous[0]}; {target.name}"
                )
            by_gvk[schema.gvk.key] = (target.name, schema)
    if not by_gvk:
        return ()
    files = {
        f"{item.gvk.group}/{item.gvk.kind.lower()}_{item.gvk.version}.json": item.content
        for _, item in by_gvk.values()
    }
    generation = content_digest(
        json.dumps(
            {name: content_digest(data) for name, data in files.items()}, sort_keys=True
        ).encode()
    ).removeprefix("sha256:")
    destination = root / "schemas" / generation
    if (
        destination.is_dir()
        and {p.relative_to(destination).as_posix() for p in destination.rglob("*.json")}
        == set(files)
        and all(
            (destination / name).is_file() and (destination / name).read_bytes() == data
            for name, data in files.items()
        )
    ):
        return (str(destination / "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"),)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination.parent))
    try:
        for name, data in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if destination.exists():
            # A damaged derived output is disposable; never modify upstream snapshots.
            shutil.rmtree(destination)
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return (str(destination / "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"),)
