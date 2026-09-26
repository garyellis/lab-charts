## 0.1.0

- Vendor stakater/reloader chart 2.2.17 (Reloader v1.4.22), digest-pinned.
- Opt-in only (`autoReloadAll: false`), cluster-wide watch, `annotations`
  reload strategy, hardened pod security context.
- Chart-owned helm test proving an annotated Deployment rolls on a Secret
  change and an unannotated one does not.
