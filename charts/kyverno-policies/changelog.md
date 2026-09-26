## 0.1.0

- Install the repository's `policies/` via the `files/policies` symlink, in
  Audit mode; `repositoryPolicies.enforce` switches individual policies to
  Enforce.
- Vendor kyverno/kyverno-policies chart 3.9.1: Pod Security Standards
  baseline as ValidatingPolicies, in Audit mode.
- Helm test asserting every installed policy is Ready.
