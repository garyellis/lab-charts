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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from chart_manager.commands.validate.schemas.crd import generate_crd_schemas
from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaStoreError,
)
from chart_manager.commands.validate.schemas.inventory import scan_rendered_directory
from chart_manager.commands.validate.schemas.models import (
    GroupVersionKind,
    MaterializedSchema,
    SchemaScope,
    content_digest,
)
from chart_manager.plumbing.errors import ChartManagerError
from chart_manager.shared.charts.chart import Chart, chart_names, load_chart
from chart_manager.shared.charts.dependencies import deps_are_fresh
from chart_manager.shared.charts.lifecycle import (
    CapabilityStatus,
    require_validation,
    validation_status,
)
from chart_manager.shared.workspace import RepositoryWorkspace

#: Render each chart in every environment, CRDs included, into ``<out>/<chart>/<env>``;
#: return each failed render's error by (chart, env).
RenderCrds = Callable[[Sequence[Chart], Path], Mapping[tuple[str, str], str]]

_LOG = logging.getLogger(__name__)


class _Fingerprints:
    """Share tool hashes within one preparation; recheck chart bytes each time."""

    def __init__(self) -> None:
        self.binaries: dict[tuple[str, int, int, int], bytes] = {}
        self.implementation: bytes | None = None
        root = Path(__file__).resolve().parents[3]
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

    def chart(self, chart: Chart) -> str | None:
        spec = require_validation(chart.lifecycle, chart_name=chart.name)
        dependencies = chart.metadata.dependencies
        if self.implementation is None or spec.helm_version:
            return None
        if dependencies and (
            not deps_are_fresh(chart.path)
            or any((dependency.repository or "").startswith("file:") for dependency in dependencies)
        ):
            return None
        binary = shutil.which(spec.helm_binary or "helm")
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
            for path in sorted(chart.path.rglob("*")):
                if path.is_symlink():
                    return None
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                digest.update(str(path.relative_to(chart.path)).encode() + b"\0")
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


def _possible_crd_provider(chart: Chart) -> bool:
    dependencies = chart.metadata.dependencies
    try:
        if dependencies and (
            not deps_are_fresh(chart.path)
            or any((dep.repository or "").startswith("file:") for dep in dependencies)
        ):
            return True
        for item in chart.path.rglob("*"):
            if item.is_symlink():
                return True
            if item.is_file() and _possible_crd_bytes(
                item.relative_to(chart.path).as_posix(), item.read_bytes()
            ):
                return True
    except OSError:
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


def providers(workspace: RepositoryWorkspace) -> list[Chart]:
    """Charts with validation enabled that may render CRDs; one that fails to load raises.

    A chart whose dependencies are stale always counts; update them first to narrow this.
    """
    charts: list[Chart] = []
    errors: list[str] = []
    for name in chart_names(workspace.charts_root):
        try:
            chart = load_chart(workspace.chart_path(name))
        except ChartManagerError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if validation_status(chart.lifecycle) is CapabilityStatus.ENABLED and (
            _possible_crd_provider(chart)
        ):
            charts.append(chart)
    if errors:
        raise KubeconformSchemaConfigurationError(
            "cannot discover CRD providers: " + "; ".join(errors)
        )
    return charts


def prepare(
    workspace: RepositoryWorkspace,
    *,
    render: RenderCrds,
    cache_root: Path,
) -> tuple[str, ...]:
    """Schema locations generated from the CRDs the repository's charts render.

    Charts whose cached schemas are current are not rendered again; `render` renders the rest.
    A failed render is logged and contributes no schemas, and its chart is not cached.
    Two charts defining one CRD differently raise.
    """
    policy = workspace.spec.validation
    if policy is None or not policy.schemas.generate_from_crds:
        return ()
    root = cache_root / "v3" / "derived"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".prepare.lock").open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                return _prepare(workspace, render, cache_root=cache_root)
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise KubeconformSchemaStoreError(f"cannot prepare derived schemas: {exc}") from exc


def _prepare(
    workspace: RepositoryWorkspace,
    render: RenderCrds,
    *,
    cache_root: Path,
) -> tuple[str, ...]:
    """Refresh changed CRD providers, reuse the rest, and publish one schema generation.

    Generated schemas are disposable build outputs and never enter schemas.lock.yaml.
    """
    policy = workspace.spec.validation
    if policy is None or not policy.schemas.generate_from_crds:
        return ()
    targets = providers(workspace)
    root = cache_root / "v3" / "derived"
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
            failures = render(uncached, output)
            if failures:
                _LOG.warning(
                    "CRD provider render failed; its CRDs are left out:\n%s",
                    "\n".join(
                        f"{chart}/{env}: {error}" for (chart, env), error in failures.items()
                    ),
                )
            for target in uncached:
                # A chart whose renders all failed may have no output directory.
                crds = [
                    crd
                    for env_dir in sorted((output / target.name).glob("*"))
                    if (target.name, env_dir.name) not in failures
                    for crd in scan_rendered_directory(
                        env_dir, scope=SchemaScope(chart=target.name, environment=env_dir.name)
                    )
                ]
                rendered[target.name] = generate_crd_schemas(crds)
        failed = {chart for chart, _ in failures}
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
            if after is not None and stable and target.name not in failed:
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
