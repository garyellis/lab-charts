"""`Kyverno.preflight` owns its version flag."""

from __future__ import annotations

from chart_manager.integrations.kyverno import Kyverno
from tests.conftest import FakeCommandRunner, OnPath


def test_kyverno_owns_its_version_flag(on_path: OnPath) -> None:
    """The surface must never learn that kyverno spells it `version`."""
    on_path("kyverno")
    runner = FakeCommandRunner(stdout="v1.13.0\n")

    Kyverno(runner).preflight()

    assert ("kyverno", "version") in runner.calls
