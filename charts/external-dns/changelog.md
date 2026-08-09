## 0.1.2

- Set `--txt-wildcard-replacement=wildcard`. Without it the TXT registry builds
  an ownership record named `a-*.<cluster>.<zone>`, which is not a legal DNS
  name, and Designate rejects it with 400 `invalid_object`. The A record lands
  but is left unowned, so `policy: sync` can neither update nor reap it and the
  create retries indefinitely. Confirmed against Designate before the fix.

## 0.1.1

- Add the `istio-gateway` source so the edge publishes each workload cluster's
  `*.<cluster>.int.garys-lab.io` record from the live ingress gateway
  LoadBalancer address, replacing the provisioning-time constant OpenTofu
  wrote. Keep `crd` for free-standing DNSEndpoint records.
- Switch `policy` from `upsert-only` to `sync`, so a changed LoadBalancer
  address is rewritten and a decommissioned cluster's record is removed.
  Deletion stays bounded by the TXT registry.

## 0.1.0

- Add ExternalDNS 0.21.0 through official chart 1.21.1.
- Configure the OpenStack Designate webhook 2.2.0 by immutable image digest.
- Limit ownership to `int.garys-lab.io` and use CRD, TXT registry, and
  upsert-only policy.
