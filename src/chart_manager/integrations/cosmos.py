"""Cosmos DB access: a cached client and document containers, configured by the caller."""

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import azure.identity
from azure.cosmos import ContainerProxy, CosmosClient

from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import PROBE_TIMEOUT, Check, first_line

#: Connection timeout for the long-lived client. Ten seconds because a write
#: is worth waiting for; `preflight` passes its own, much shorter budget.
_CLIENT_TIMEOUT = 10


@dataclass(frozen=True, slots=True)
class CosmosConfig:
    """Where one Cosmos container is and how to reach it."""

    endpoint: str
    database: str
    container: str
    #: Master key (emulator, local development). None: Entra ID through
    #: DefaultAzureCredential, which picks up `az login`, a VM's managed
    #: identity, AKS workload identity or `AZURE_*` service-principal env vars.
    key: str | None = field(default=None, repr=False)
    ca_bundle: Path | None = None
    verify_tls: bool = True


def _build_client(config: CosmosConfig, connection_timeout: float) -> CosmosClient:
    """Build a CosmosClient for `config` with one timeout.

    Split out of `get_cosmos_client` so `preflight` can build a short-timeout
    client that authenticates exactly as the writer does.
    """
    options: dict[str, Any] = {
        "enable_endpoint_discovery": False,
        "connection_timeout": connection_timeout,
    }
    if config.ca_bundle is not None:
        options["connection_verify"] = str(config.ca_bundle)
    elif not config.verify_tls:
        options["connection_verify"] = False
    credential = config.key or azure.identity.DefaultAzureCredential()
    return CosmosClient(config.endpoint, credential=credential, **options)


@functools.lru_cache(maxsize=1)
def get_cosmos_client(config: CosmosConfig) -> CosmosClient:
    """Build (once) the CosmosClient every write goes through."""
    return _build_client(config, _CLIENT_TIMEOUT)


class CosmosContainer:
    """One Cosmos container, read and written as JSON documents."""

    def __init__(self, proxy: ContainerProxy) -> None:
        """Bind the SDK container handle."""
        self._proxy = proxy

    def write(self, item: dict[str, Any], *, upsert: bool) -> None:
        """Upsert `item` (replacing a document with the same id), or create it."""
        if upsert:
            self._proxy.upsert_item(item)
        else:
            self._proxy.create_item(item)

    def query(
        self, sql: str, parameters: list[dict[str, Any]], partition_key: str | None
    ) -> list[dict[str, Any]]:
        """Run `sql` in one partition, or across all of them when `partition_key` is None."""
        addressing: dict[str, Any] = (
            {"enable_cross_partition_query": True}
            if partition_key is None
            else {"partition_key": partition_key}
        )
        items = self._proxy.query_items(query=sql, parameters=parameters, **addressing)
        # `_`-prefixed keys are Cosmos bookkeeping (_rid, _etag, _ts, ...):
        # transport metadata, not document content.
        return [
            {key: value for key, value in item.items() if not key.startswith("_")}
            for item in items
        ]


def get_container(config: CosmosConfig) -> CosmosContainer:
    """Return a handle on an existing container; nothing is created."""
    db = get_cosmos_client(config).get_database_client(config.database)
    return CosmosContainer(db.get_container_client(config.container))


def preflight(config: CosmosConfig, *, timeout: float = PROBE_TIMEOUT) -> Check:
    """Report whether this process could write lifecycle events to Cosmos.

    Builds its own client rather than reusing the cached one, so the probe
    is capped at `timeout` instead of the writer's ten seconds.
    """
    target = f"cosmos {config.database}/{config.container}"
    try:
        client = _build_client(config, timeout)
        client.get_database_client(config.database).get_container_client(config.container).read()
    except Exception as exc:
        # Any SDK, auth, TLS or network failure is one report to the
        # operator. Narrowing this would only add branches that produce the
        # same sentence, and a preflight that raises is a preflight that
        # cannot report the other checks.
        return Check.failed(
            "events-backend",
            f"{target} unreachable: {first_line(str(exc)) or type(exc).__name__}",
            remediation=(
                "check the endpoint and credentials; create a missing database "
                "or container with `mise run events:provision`"
            ),
            outcome=Outcome.ENVIRONMENT,
        )
    return Check.ok("events-backend", f"{target} reachable")
