"""Releases the environment bootstrap installs before any chart under test."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExternallySatisfiedLifecycle:
    """Exact managed lifecycle identity already converged by an environment."""

    chart_path: Path
    chart: str
    profile: str
    namespace: str
