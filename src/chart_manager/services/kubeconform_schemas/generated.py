"""Derived CRD schemas cached independently of upstream repository pins."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaRenderError,
    KubeconformSchemaStoreError,
)
from chart_manager.commands.validate.schemas.models import (
    GroupVersionKind,
    MaterializedSchema,
    SchemaScope,
    content_digest,
)
from chart_manager.commands.validate.schemas.store import default_schema_cache_root
from chart_manager.plumbing.errors import SpecError
from chart_manager.services.kubeconform_schemas.crd import generate_crd_schemas
from chart_manager.services.kubeconform_schemas.inventory import scan_rendered_directory
from chart_manager.services.manifest_validation.catalog import build_catalog
from chart_manager.services.manifest_validation.models import ManifestValidationTarget, RunRequest
from chart_manager.shared.charts.chart import ChartRepository, load_chart_metadata
from chart_manager.shared.charts.dependencies import deps_are_fresh
from chart_manager.shared.workspace import RepositoryWorkspace

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


def _authored_fingerprint(path: Path) -> str | None:
    """Check nondependency inputs across Helm's dependency preparation.

    Helm may materialize charts/ and create/update Chart.lock during render.
    Those bytes enter the final full fingerprint; every other input must stay
    unchanged before we can associate the rendered schemas with that hash.
    """
    digest = hashlib.sha256()
    try:
        for item in sorted(path.rglob("*")):
            relative = item.relative_to(path)
            if relative.parts[0] == "charts" or relative == Path("Chart.lock"):
                continue
            if item.is_symlink():
                return None
            if item.is_file():
                digest.update(relative.as_posix().encode() + b"\0")
                digest.update(hashlib.sha256(item.read_bytes()).digest())
    except OSError:
        return None
    return digest.hexdigest()


def _possible_crd_bytes(name: str, data: bytes, *, depth: int = 0) -> bool:
    """Conservative discovery, including packaged and nested dependencies.

    Never extract archives. Unreadable/oversize inputs and templates without a
    statically declared resource kind remain potential providers and render.
    """
    if name.endswith(".tgz"):
        if depth >= 8 or len(data) > 64 * 1024 * 1024:
            return True
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                total = 0
                for index, member in enumerate(archive):
                    total += member.size
                    if index >= 4096 or total > 128 * 1024 * 1024:
                        return True
                    if member.issym() or member.islnk():
                        return True
                    if not member.isfile():
                        continue
                    stream = archive.extractfile(member)
                    if stream is None or _possible_crd_bytes(
                        member.name, stream.read(), depth=depth + 1
                    ):
                        return True
        except (OSError, EOFError, tarfile.TarError):
            return True
        return False
    if b"CustomResourceDefinition" in data or b"apiextensions.k8s.io" in data:
        return True
    parts = Path(name).parts
    if "crds" in parts and data.strip():
        return True
    if (
        "templates" in parts
        and not Path(name).name.startswith("_")
        and Path(name).name != "NOTES.txt"
    ):
        controls = re.compile(rb"^(?:if|else|end|range|with|define)\b|^/\*|^\$[\w, $]+\s*(?::=|=)")
        indented = re.compile(rb"\|\s*n?indent\s+[1-9][0-9]*\s*-?\s*$")
        # Inspect literal documents independently: an indented whole resource
        # after --- is still a provider, whereas a positively indented helper
        # within a static resource cannot inject another YAML document.
        # Controls can precede a separator on the same source line. Split
        # any literal separator conservatively, including quoted/commented
        # occurrences, rather than assume only raw line-start --- matters.
        for document in data.split(b"---"):
            kinds = re.findall(rb"(?m)^kind:\s*([^\r\n]*)", document)
            if any(b"{{" in kind for kind in kinds):
                return True
            previous_end = 0
            for match in re.finditer(rb"{{-?\s*(.*?)}}", document, re.DOTALL):
                prefix = document[previous_end : match.start()].rsplit(b"\n", 1)[-1]
                previous_end = match.end()
                if controls.match(match[1]):
                    continue
                if not kinds:
                    return True
                if not prefix.strip() and not indented.search(match[1]):
                    return True
    return False


def _possible_crd_provider(path: Path) -> bool:
    try:
        metadata = load_chart_metadata(path / "Chart.yaml")
        if metadata.dependencies and (
            not deps_are_fresh(path)
            or any((dep.repository or "").startswith("file:") for dep in metadata.dependencies)
        ):
            return True
        for item in path.rglob("*"):
            if item.is_symlink():
                return True
            if item.is_file() and _possible_crd_bytes(
                item.relative_to(path).as_posix(), item.read_bytes()
            ):
                return True
    except (OSError, SpecError):
        return True
    return False


def _load_cached(path: Path) -> tuple[MaterializedSchema, ...] | None:
    try:
        wrapper = json.loads(path.read_bytes())
        payload = wrapper["payload"]
        if content_digest(payload.encode()) != wrapper["checksum"]:
            return None
        return tuple(
            MaterializedSchema(
                gvk=GroupVersionKind.model_validate(item["gvk"]),
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
    policy = workspace.spec.validation
    if policy is None or not policy.schemas.generate_from_crds:
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
    policy = workspace.spec.validation
    if policy is None or not policy.schemas.generate_from_crds:
        return ()
    repository = ChartRepository(workspace.root, charts_dir=workspace.spec.charts_dir)
    providers = [
        name
        for name in repository.list_names()
        if _possible_crd_provider(repository.charts_dir / name)
    ]
    catalog = build_catalog(
        workspace.root, chart_names=providers, charts_dir=workspace.spec.charts_dir
    )
    if catalog.errors:
        raise KubeconformSchemaConfigurationError(
            "cannot discover CRD providers: " + "; ".join(catalog.errors)
        )
    validation.prepare_schema_dependencies(catalog.targets)
    targets = [target for target in catalog.targets if _possible_crd_provider(target.path)]
    root = (cache_root or default_schema_cache_root()) / "v3" / "derived"
    by_gvk: dict[str, tuple[str, MaterializedSchema]] = {}
    fingerprints = _Fingerprints()
    prepared = []
    for target in targets:
        fingerprint = fingerprints.chart(target)
        cache = root / "charts" / f"{fingerprint}.json" if fingerprint else None
        schemas = _load_cached(cache) if cache is not None else None
        prepared.append((target, fingerprint, cache, schemas))
    cached = sum(schemas is not None for _, _, _, schemas in prepared)
    _LOG.info(
        "Preparing CRD schemas: %d providers cached, %d to render",
        cached,
        len(prepared) - cached,
    )
    uncached = [target for target, _, _, schemas in prepared if schemas is None]
    rendered: dict[str, tuple[MaterializedSchema, ...]] = {}
    if uncached:
        authored = {target.name: _authored_fingerprint(target.path) for target in uncached}
        with tempfile.TemporaryDirectory(prefix="chart-manager-crds-") as temporary:
            output = Path(temporary)
            outcome = validation.run(
                RunRequest(
                    root=workspace.root,
                    charts=tuple(target.name for target in uncached),
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
            for target in uncached:
                crds = [
                    crd
                    for row in outcome.result.rows
                    if row.row.chart == target.name
                    for crd in scan_rendered_directory(
                        output / row.row.chart / row.row.env,
                        scope=SchemaScope(chart=row.row.chart, environment=row.row.env),
                    )
                ]
                rendered[target.name] = generate_crd_schemas(crds)
        for target, fingerprint, _cache, schemas in prepared:
            if schemas is not None:
                continue
            schemas = rendered[target.name]
            after = fingerprints.chart(target)
            stable = (
                fingerprint == after
                if fingerprint
                else (
                    authored[target.name] is not None
                    and authored[target.name] == _authored_fingerprint(target.path)
                )
            )
            # A cold render hydrates dependencies; publish against those final
            # bytes on this very run, provided authored inputs stayed stable.
            if after is not None and stable:
                _write_cached(root / "charts" / f"{after}.json", schemas)
    for target, _, _, schemas in prepared:
        for schema in schemas if schemas is not None else rendered[target.name]:
            previous = by_gvk.get(schema.gvk.key)
            if previous and previous[1].content != schema.content:
                raise KubeconformSchemaConfigurationError(
                    f"conflicting rendered CRDs define {schema.gvk.key}: "
                    f"{previous[0]}; {target.name}"
                )
            by_gvk[schema.gvk.key] = (target.name, schema)
    active = []
    for target, _, _, _ in prepared:
        fingerprint = fingerprints.chart(target)
        if fingerprint and (root / "charts" / f"{fingerprint}.json").is_file():
            active.append(f"{fingerprint}.json")
    manifest = root / "current-charts.json"
    fd, temporary = tempfile.mkstemp(prefix=".staging-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(sorted(set(active)), stream)
        os.replace(temporary, manifest)
    finally:
        Path(temporary).unlink(missing_ok=True)
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
