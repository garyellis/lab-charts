"""Resolve a `local` target to a chart directory or a loaded `LocalStack`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from chart_manager.api.v1alpha1.local_stack import LocalStack
from chart_manager.plumbing.errors import SpecError
from chart_manager.plumbing.names import dns_label
from chart_manager.plumbing.paths import inside_root, relative_path
from chart_manager.shared.charts.chart import ResolvedChartTarget, chart_target
from chart_manager.shared.cluster.local_cluster import load_resource, validate_release

DEFAULT_STACKS_DIR = Path("stacks")


class ResolvedStackTarget(BaseModel):
    """A loaded stack and its canonical authored source."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["stack"] = "stack"
    name: str
    path: Path
    stack: LocalStack


type ResolvedLocalTarget = ResolvedChartTarget | ResolvedStackTarget


class LocalTargetResolver:
    """Resolve a repository chart directory or a named/explicit ``LocalStack``."""

    def __init__(
        self,
        root: Path,
        *,
        local_config: Path,
        stacks_dir: Path = DEFAULT_STACKS_DIR,
    ) -> None:
        self.root = root.resolve()
        self.local_config = relative_path(local_config, field="local_config")
        self.stacks_dir = relative_path(stacks_dir, field="stacks_dir")

    @property
    def stacks_path(self) -> Path:
        return self.root / self.local_config.parent / self.stacks_dir

    def load_stack(self, path: Path) -> LocalStack:
        stack = load_resource(inside_root(self.root, path), LocalStack)
        for release in stack.spec.releases:
            validate_release(self.root, release)
        return stack

    def resolve(self, target: str | Path) -> ResolvedLocalTarget:
        raw = str(target)
        if not raw or raw != raw.strip():
            raise SpecError("local target must be a non-empty chart path or stack name")
        candidate = Path(raw)
        explicit = candidate if candidate.is_absolute() else self.root / candidate
        if explicit.exists():
            return self._resolve_explicit(explicit)

        if candidate.is_absolute() or len(candidate.parts) != 1 or candidate.suffix:
            raise SpecError(f"local target path does not exist: {candidate}")
        try:
            name = dns_label(raw, field="LocalStack name")
        except ValueError as exc:
            raise SpecError(str(exc)) from exc
        stack_path = self.stacks_path / f"{name}.yaml"
        if not stack_path.is_file():
            raise SpecError(f"unknown LocalStack {name!r}: expected {stack_path}")
        resolved = self._resolve_stack(stack_path)
        if resolved.name != name:
            raise SpecError(
                f"{stack_path} metadata.name {resolved.name!r} does not match stack name {name!r}"
            )
        return resolved

    def _resolve_explicit(self, path: Path) -> ResolvedLocalTarget:
        absolute = inside_root(self.root, path)
        if absolute.is_dir():
            return chart_target(self.root, absolute)
        if absolute.is_file():
            return self._resolve_stack(absolute)
        raise SpecError(f"local target is neither a chart directory nor LocalStack file: {path}")

    def _resolve_stack(self, path: Path) -> ResolvedStackTarget:
        if path.suffix not in {".yaml", ".yml"}:
            raise SpecError(f"LocalStack file must use .yaml or .yml: {path}")
        stack = self.load_stack(path)
        return ResolvedStackTarget(name=stack.metadata.name, path=path.resolve(), stack=stack)


__all__ = [
    "DEFAULT_STACKS_DIR",
    "LocalTargetResolver",
    "ResolvedLocalTarget",
    "ResolvedStackTarget",
]
