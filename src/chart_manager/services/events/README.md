# notes about events service

## expose as CLI commands to invoke from CI server itself.

`chart-manager publish` emits these build transitions automatically after
successful OCI pushes. A publish using `--version-suffix` is a PR/preview
artifact and emits `preview_published`; an unsuffixed publish emits
`published`. Use `--kind` to state the intent explicitly. Push
failures emit nothing, event failures are non-fatal unless the CLI is invoked
with `--strict-events`, and retries are deduplicated by phase, chart, version,
and artifact digest.

```
  # build lifecycle (charts repo CI)
  chart-manager event emit build redis@1.2.0 \
    --phase published \
    --build-correlation-id "$GITHUB_REPOSITORY#$PR_NUMBER" \
    --pr-url "$PR_URL" --git-sha "$GITHUB_SHA"

  # promotion lifecycle (flux repo CI)
  chart-manager event emit promote redis@1.2.0 --env dev \
    --phase reached_prod --pr-url "$PR_URL"
```

The `chart@version` positional is parsed by `ref.py`, not by the CLI: it is
already the `correlation_id` the writer composes, so its grammar belongs to
this package. The pre-P1 spelling (`events build|promote`, with `--chart` and
`--version` as separate flags) still works as a hidden deprecated alias and is
removed in 0.6.

## python service bindings
continue adding to helmrelease actions. test, monitor, and promote. the process/instance
won't know when PR is merged, so exposing as command is useful for this scenario or we can walso add it from a pr watcher service
