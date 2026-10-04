"""Wire contract for the GitHub Actions cluster-test matrix.

This module is the single source of truth for the machine-readable shape CI
consumes. `.github/workflows/ci.yaml` captures the emitting command's stdout
into a shell variable and feeds it to `strategy.matrix`, so the payload's
shape is an external contract with GitHub Actions, not an internal detail.
Built in the CLI it was invisible to `services/`; a REST or Slack surface
that wanted to hand the same matrix to a workflow would have had to copy the
dict literal out of `cli/main.py`.

GitHub fixes the `matrix.include` shape. Adding a key to an entry adds a
`matrix.<key>` to every job; renaming `chart`/`profile` breaks the jobs that
reference `matrix.chart` or `matrix.profile`.

Deliberately I/O-free and format-free, matching the other wire modules:
these functions return plain dicts. Choosing an encoder -- `json.dumps`
separators, YAML, an HTTP response body -- and performing the write is the
surface's job.

The *selection* of which entries belong in the matrix is a separate concern
and lives in `test.select()`. This module only shapes what selection returned.
"""

from __future__ import annotations

from collections.abc import Sequence

from chart_manager.commands import test

__all__ = ["cluster_test_matrix_to_dict"]


def cluster_test_matrix_to_dict(
    entries: Sequence[test.SelectedTest],
) -> dict[str, list[dict[str, str]]]:
    """Project selected matrix entries onto the GitHub Actions matrix payload.

    Deliberately narrower than `lifecycle/wire.py`'s `impact_to_dict`: the selection
    `reasons` are why a chart was chosen, which is useful to a human reading
    `ci impact` and meaningless to `strategy.matrix`. Including them would
    add a `matrix.reasons` dimension to every job.
    """
    return {
        "include": [
            {"chart": entry.chart, "profile": entry.profile} for entry in entries
        ]
    }
