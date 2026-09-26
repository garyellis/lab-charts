## 0.1.0

- Vendor kyverno/kyverno chart 3.9.1 (Kyverno v1.19.1).
- Admission and reports controllers, one replica each; background and
  cleanup controllers off.
- Resource webhooks forced to `failurePolicy: Ignore`; PolicyExceptions
  accepted from the `kyverno` namespace only.
- Chart-owned helm test proving a Deny-mode ValidatingPolicy admits a
  compliant Pod and rejects its violating twin.
