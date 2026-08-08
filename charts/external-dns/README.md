# ExternalDNS

Thin umbrella around the official ExternalDNS chart. The live configuration
runs only in `edge-w`, consumes `DNSEndpoint` resources, and writes exclusively
to the internal Designate zone `int.garys-lab.io` through the OpenStack webhook.

The webhook expects an existing Secret named `external-dns-openstack` with a
`clouds.yaml` key and an `openstack` cloud using a least-privilege application
credential. This chart never creates or stores that credential.

Record deletion is initially disabled through `policy: upsert-only`. Change to
`sync` only after live create, update, ownership, and deletion tests pass.

The Designate webhook authenticates to Keystone during process startup, so an
offline kind cluster cannot provide a truthful workload-ready test. CI performs
render, schema, and policy validation. The edge cluster provides the required
live create/update/ownership acceptance test with a least-privilege credential.

The first live acceptance must keep `policy: upsert-only`, create one disposable
record inside `int.garys-lab.io`, verify its TXT ownership record, update its
target, and confirm that removing the source does not delete it. Test deletion
separately before enabling `sync`.
