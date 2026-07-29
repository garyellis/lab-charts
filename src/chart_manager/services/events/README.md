# notes about events service

## expose as CLI commands to invoke from CI server itself.

`chart-manager publish` emits these build transitions automatically after
successful OCI pushes. A publish using `--version-suffix` is a PR/preview
artifact and emits `preview_published`; an unsuffixed publish emits
`published`. Use `--publish-kind` to state the intent explicitly. Push
failures emit nothing, event failures are non-fatal unless the CLI is invoked
with `--strict-events`, and retries are deduplicated by phase, chart, version,
and artifact digest.

```
  # build lifecycle (charts repo CI)
  chart-manager events build \
    --chart redis --version 1.2.0 \
    --phase published \
    --build-correlation-id "$GITHUB_REPOSITORY#$PR_NUMBER" \
    --pr-url "$PR_URL" --git-sha "$GITHUB_SHA"

  # promotion lifecycle (flux repo CI)
  chart-manager events promote \
    --chart redis --version 1.2.0 --environment dev \
    --phase reached_prod --pr-url "$PR_URL"
```

## python service bindings
continue adding to helmrelease actions. test, monitor, and promote. the process/instance
won't know when PR is merged, so exposing as command is useful for this scenario or we can walso add it from a pr watcher service
