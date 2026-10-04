import pytest

from chart_manager.commands.validate.schemas.errors import KubeconformSchemaLockError
from chart_manager.commands.validate.schemas.lock import (
    load_schema_lock,
    serialize_schema_lock,
    write_schema_lock_atomic,
)
from chart_manager.commands.validate.schemas.models import RepositoryPin, build_lock

from .schema_fixtures import schema_store


def test_lock_round_trips_without_any_chart_inventory(tmp_path):
    lock, _, _ = schema_store(tmp_path)
    path = tmp_path / "schemas.lock.yaml"
    write_schema_lock_atomic(path, lock)
    assert load_schema_lock(path) == lock
    text = serialize_schema_lock(lock)
    assert path.read_text() == text
    assert "version: 2\n" in text
    assert "schemas:" not in text
    assert "inventory:" not in text
    assert len(text.splitlines()) < 20


@pytest.mark.parametrize("mutation", ["digest", "schema-list", "old-version", "yaml"])
def test_invalid_lock_is_rejected(tmp_path, mutation):
    lock, _, _ = schema_store(tmp_path)
    text = serialize_schema_lock(lock)
    if mutation == "digest":
        text = text.replace(lock.generation, "sha256:" + "0" * 64)
    elif mutation == "schema-list":
        text += "schemas: []\n"
    elif mutation == "old-version":
        text = text.replace("version: 2", "version: 1")
    else:
        text = "broken: [\n"
    path = tmp_path / "lock.yaml"
    path.write_text(text)
    with pytest.raises(KubeconformSchemaLockError):
        load_schema_lock(path)


@pytest.mark.parametrize(
    "repo,revision", [("../evil", "a" * 40), ("x/y/z", "a" * 40), ("x/y", "main")]
)
def test_pins_reject_unsafe_or_moving_identities(repo, revision):
    with pytest.raises(ValueError):
        RepositoryPin(repository=repo, resolved=revision, track="main")


def test_only_policy_and_pins_determine_lock_identity(tmp_path):
    lock, _, _ = schema_store(tmp_path)
    assert build_lock(workspace=lock.workspace, policy=lock.policy) == lock
    policy = lock.policy.model_copy(update={"kubernetes_version": "1.36.0"})
    assert build_lock(workspace=lock.workspace, policy=policy).generation != lock.generation
