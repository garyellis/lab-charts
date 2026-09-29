"""The authored ``LocalStack`` contract."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import Field

from chart_manager.api.v1alpha1.common import ApiVersion, StrictApiModel
from chart_manager.api.v1alpha1.releases import ResourceMetadata, StackRelease

LocalStackKind = Literal["LocalStack"]
LOCAL_STACK_KIND: LocalStackKind = get_args(LocalStackKind)[0]


class LocalStackSpec(StrictApiModel):
    """Releases composing one stack, at least one.

    Typed as `StackRelease`, not `BootstrapRelease`: a `type: local` release is
    rejected here. A stack must stay reusable, so every release names either a
    lifecycle profile, a pinned OCI chart, or an exactly versioned chart from
    an HTTPS Helm repository.
    """

    releases: list[StackRelease] = Field(min_length=1)


class LocalStack(StrictApiModel):
    """Envelope for a reusable application composition.

    Resolved by name from the `stacks/` directory beside the LocalCluster file,
    or from an explicit path. Composition only -- no templating or ordering
    language, which is what keeps it narrower than Helmfile.
    """

    api_version: ApiVersion = Field(alias="apiVersion")
    kind: LocalStackKind
    metadata: ResourceMetadata
    spec: LocalStackSpec
