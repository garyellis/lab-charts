"""Small real Git schema repositories for hermetic snapshot contract tests."""

from pathlib import Path

from chart_manager.domain.workspace import RepositoryWorkspace
from chart_manager.integrations.kubeconform.repository_snapshot import RepositorySnapshot
from chart_manager.plumbing.commands import SubprocessRunner
from chart_manager.services.kubeconform_schemas.models import (
    LockedSchemaPolicy,
    RepositoryPin,
    build_lock,
)
from chart_manager.services.kubeconform_schemas.store import KubeconformSchemaStore
from tests.conftest import workspace_for


def git(root: Path, *args: str) -> str:
    return SubprocessRunner().run(["git", *args], cwd=root).stdout.strip()


def repository(root: Path, files: dict[str, str]) -> str:
    root.mkdir(parents=True)
    git(root, "init", "--quiet")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--quiet",
        "-m",
        "schemas",
    )
    return git(root, "rev-parse", "HEAD")


def workspace(root: Path) -> RepositoryWorkspace:
    return workspace_for(
        root,
        name="lab",
        validation={
            "kubernetesVersion": "1.35.3",
            "schemas": {
                "generateFromCRDs": True,
                "catalog": {"repository": "datreeio/CRDs-catalog", "track": "main"},
            },
        },
    )


class LocalSnapshots(RepositorySnapshot):
    def __init__(self, repositories: dict[str, Path]) -> None:
        super().__init__()
        self.repositories = repositories
        self.calls: list[str] = []

    def checkout(self, repository, revision, destination, *, directory=None):
        self.calls.append(repository)
        git(
            destination,
            "clone",
            "--quiet",
            "--no-checkout",
            str(self.repositories[repository]),
            ".",
        )
        if directory:
            git(destination, "sparse-checkout", "set", "--cone", directory)
        git(destination, "checkout", "--quiet", "--detach", revision)


def schema_store(tmp_path: Path):
    kubernetes = tmp_path / "upstream-kubernetes"
    catalog = tmp_path / "upstream-catalog"
    ksha = repository(
        kubernetes,
        {
            "v1.35.3-standalone-strict/deployment-apps-v1.json": '{"type":"object"}',
            "v1.35.3-standalone-strict/poddisruptionbudget-policy-v1.json": '{"type":"object"}',
            "v1.35.3-standalone-strict/configmap-v1.json": '{"type":"object"}',
            "v1.34.0-standalone-strict/configmap-v1.json": '{"type":"object"}',
        },
    )
    csha = repository(catalog, {"example.io/widget_v1.json": '{"type":"object"}'})
    lock = build_lock(
        workspace="lab",
        policy=LockedSchemaPolicy(
            kubernetes_version="1.35.3",
            generate_from_crds=True,
            kubernetes=RepositoryPin(
                repository="yannh/kubernetes-json-schema", track="master", resolved=ksha
            ),
            catalog=RepositoryPin(repository="datreeio/CRDs-catalog", track="main", resolved=csha),
        ),
    )
    snapshots = LocalSnapshots(
        {lock.policy.kubernetes.repository: kubernetes, lock.policy.catalog.repository: catalog}
    )
    store = KubeconformSchemaStore(cache_root=tmp_path / "cache", snapshots=snapshots)
    return lock, store, snapshots
