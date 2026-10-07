"""Shared setup for integration tests that run `validate.run()` with the real tools."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from chart_manager.plumbing.yaml_files import dump_yaml, load_yaml_file

FIXTURES = Path(__file__).parent.parent / "fixtures"


def require(*tools: str) -> None:
    """Fail the test unless every tool is on PATH."""
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.fail(f"missing tools on PATH: {', '.join(missing)}")


def fixture_chart(root: Path, name: str, **validation: Any) -> Path:
    """Copy `tests/fixtures/charts/<name>` into `root/charts`, merging `validation` into its lifecycle."""
    chart = root / "charts" / name
    shutil.copytree(FIXTURES / "charts" / name, chart)
    lifecycle = chart / "chart-lifecycle.yaml"
    document = load_yaml_file(lifecycle)
    document["spec"]["validation"].update(validation)
    lifecycle.write_text(dump_yaml(document))
    return chart
