"""Atomic local checkouts of pinned upstream schema repositories."""

from __future__ import annotations

import fcntl
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from chart_manager.commands.validate.schemas.errors import (
    KubeconformSchemaConfigurationError,
    KubeconformSchemaSourceEnvironmentError,
    KubeconformSchemaStoreError,
)
from chart_manager.commands.validate.schemas.models import RepositoryPin, SchemaLock
from chart_manager.integrations.kubeconform.repository_snapshot import (
    RepositorySnapshot,
    RepositorySnapshotDirectoryNotFoundError,
)
from chart_manager.plumbing.commands import CommandRunner
from chart_manager.plumbing.errors import ExternalCommandError


@dataclass(frozen=True)
class StoreProblem:
    path: str
    detail: str


@dataclass(frozen=True)
class StoreStatus:
    generation_path: Path
    expected: int
    present: int
    missing: tuple[StoreProblem, ...]
    corrupt: tuple[StoreProblem, ...]

    @property
    def ready(self) -> bool:
        return not self.missing and not self.corrupt


class KubeconformSchemaStore:
    """Share immutable repository checkouts across charts and workspaces."""

    def __init__(
        self,
        *,
        cache_root: Path,
        snapshots: RepositorySnapshot,
    ) -> None:
        self.cache_root = cache_root.resolve()
        self.root = self.cache_root / "v3"
        self.snapshots = snapshots

    def repository_path(self, pin: RepositoryPin, directory: str | None = None) -> Path:
        return self.root / "repositories" / pin.repository / pin.resolved / (directory or "all")

    def repositories(self, lock: SchemaLock) -> tuple[tuple[RepositoryPin, str | None], ...]:
        return (
            (lock.policy.kubernetes, f"v{lock.policy.kubernetes_version}-standalone-strict"),
            (lock.policy.catalog, None),
        )

    def generation_path(self) -> Path:
        # A policy selects two independently reusable repository checkouts.
        return self.root / "repositories"

    def inspect(self, lock: SchemaLock) -> StoreStatus:
        missing: list[StoreProblem] = []
        corrupt: list[StoreProblem] = []
        present = 0
        for pin, directory in self.repositories(lock):
            path = self.repository_path(pin, directory)
            if not path.exists():
                missing.append(StoreProblem(str(path), "repository snapshot is missing"))
                continue
            problem = self.snapshots.inspect(path, pin.resolved, directory=directory)
            if problem:
                corrupt.append(StoreProblem(str(path), problem))
            else:
                present += 1
        return StoreStatus(self.generation_path(), 2, present, tuple(missing), tuple(corrupt))

    def sync(self, lock: SchemaLock) -> bool:
        """Hydrate missing repositories without consulting any chart inputs."""
        published = False
        with self.serialized_sync():
            for pin, directory in self.repositories(lock):
                destination = self.repository_path(pin, directory)
                if destination.exists():
                    problem = self.snapshots.inspect(destination, pin.resolved, directory=directory)
                    if problem:
                        raise KubeconformSchemaStoreError(
                            f"{destination}: {problem}; remove this snapshot and run "
                            "`chart-manager schemas sync`"
                        )
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination.parent))
                try:
                    self.snapshots.checkout(
                        pin.repository, pin.resolved, stage, directory=directory
                    )
                    stage.rename(destination)
                    published = True
                except RepositorySnapshotDirectoryNotFoundError as exc:
                    raise KubeconformSchemaConfigurationError(
                        f"{exc}; choose a Kubernetes version available at the pinned commit "
                        "or update the upstream pin with `chart-manager schemas sync --update`"
                    ) from exc
                except ExternalCommandError as exc:
                    raise KubeconformSchemaSourceEnvironmentError(
                        f"cannot cache {pin.repository}@{pin.resolved}: {exc}"
                    ) from exc
                except OSError as exc:
                    raise KubeconformSchemaStoreError(
                        f"cannot publish {destination}: {exc}"
                    ) from exc
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
        return published

    @contextmanager
    def serialized_sync(self) -> Iterator[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".sync.lock").open("a+b") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise KubeconformSchemaStoreError(
                f"cannot write schema cache {self.root}: {exc}"
            ) from exc

    def locations(self, lock: SchemaLock) -> tuple[str, ...]:
        kubernetes, catalog = self.repositories(lock)
        return (
            str(
                self.repository_path(*kubernetes)
                / str(kubernetes[1])
                / "{{.ResourceKind}}{{.KindSuffix}}.json"
            ),
            str(
                self.repository_path(*catalog)
                / "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
            ),
        )


def open_schema_store(runner: CommandRunner, schema_cache_root: Path) -> KubeconformSchemaStore:
    """The schema store under `schema_cache_root`, checking out snapshots through `runner`."""
    return KubeconformSchemaStore(
        cache_root=schema_cache_root, snapshots=RepositorySnapshot(runner)
    )
