"""Lexical name validation shared by authored API models and service code.

`dns_label` is a pure `str -> str` rule with no knowledge of charts, clusters
or the repository, which is why it lives here rather than in either of the two
layers that use it.  `chart_manager.api.local.v1alpha1` applies it to authored
fields (`metadata.name`, `release.name`, `release.namespace`, ...) and
`chart_manager.services.local_resources` applies the same rule to a stack name
typed on the command line and to a name read out of `Chart.yaml`.  Keeping one
definition means the two can never drift into accepting different spellings.

It raises `ValueError`, not `SpecError`: the API layer must not raise service
exceptions, and the loaders that call it directly translate the failure
themselves.
"""

from __future__ import annotations

import re

__all__ = ["dns_label"]

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


def dns_label(value: str, *, field: str) -> str:
    """Require `value` to be a lowercase DNS label of at most 63 characters."""
    if len(value) > 63 or not _DNS_LABEL.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase DNS label of at most 63 characters")
    return value
