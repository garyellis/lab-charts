# notes about events service

## expose as CLI commands to invoke from CI server itself.

```
  # build lifecycle (charts repo CI)
  chart-manager events build \
    --chart redis --version 1.2.0 \
    --phase published \
    --build-correlation-id "$PR_URL" --git-sha "$GITHUB_SHA"

  # promotion lifecycle (flux repo CI)
  chart-manager events promote \
    --chart redis --version 1.2.0 --environment dev \
    --phase reached_prod --pr-url "$PR_URL"
```

## python service bindings
continue adding to helmrelease actions. test, monitor, and promote. the process/instance
won't know when PR is merged, so exposing as command is useful for this scenario or we can walso add it from a pr watcher service
