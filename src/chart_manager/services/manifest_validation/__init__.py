"""Manifest-validation pipeline: render -> schema -> policy.

Architectural contract, by owner:

    `models.py`    — the vocabulary: rows, phase results, the request a
                     surface hands in and the outcome it gets back, plus the
                     folds (tally, no-work reason) every projection shares.
    `validators.py`
                   — the validator contracts: ids, categories, config
                     shapes, and the two protocols a validator implements.
    `validator_adapters.py`
                   — the built-in validators end to end: input resolution,
                     execution against a rendered dir, and the registry.
    `runner.py`    — renders each row, sequences its gates, and fans rows out
                     across workers. Does not interpret results beyond
                     aggregating them into a `RunResult`.
    `paths.py`     — where rendered output goes, and what `cache clean`
                     removes. Every containment check lives here.
    `catalog.py`   — discovers charts with manifest-validation specifications.
    `planner.py`   — selects chart/environment rows from changes and filters.
    `resolver.py`  — resolves authored inputs into runtime paths and options.
    `app.py`       — `ManifestValidationService`: the env-aware layer. Changed-file
                     resolution, render-dir paths, run ids, summary
                     artifacts, retention/cleanup, worker counts, helm
                     bindings.
    `wire.py`      — the versioned machine-readable JSON contract.
    `markdown.py`  — the GitHub-flavored markdown rendering of a run.
    `progress.py`  — the progress port surfaces plug a display into.

The CLI owns exactly one thing this package does not: terminating the
process. Everything else it used to own — paths, run ids, cleanup — moved
into `ManifestValidationService` so that a REST worker or Slack handler drives the same
pipeline without reimplementing them.

This split is what lets us unit-test phases without subprocess mocking
pyramids, and lets the runner stay reusable from both `validate chart`
and `validate run`.
"""
