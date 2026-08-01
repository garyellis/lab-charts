"""Cosmos DB access: cached client and container handles, env-var configured."""

import functools
import os
from typing import Any

import azure.identity
from azure.cosmos import ContainerProxy, CosmosClient, PartitionKey, exceptions

from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import PROBE_TIMEOUT, Check, first_line

#: Connection timeout for the long-lived client. Ten seconds because a write
#: is worth waiting for; `preflight` passes its own, much shorter budget.
_CLIENT_TIMEOUT = 10


def _build_client(connection_timeout: float) -> CosmosClient:
    """Build a CosmosClient with this module's auth precedence and one timeout.

    Auth precedence: COSMOS_CONNECTION_STRING > COSMOS_KEY > DefaultAzureCredential
    against COSMOS_ENDPOINT. COSMOS_CA_BUNDLE / COSMOS_VERIFY_TLS tune TLS
    verification for the emulator.

    Split out of `get_cosmos_client` so `preflight` can build a short-timeout
    client without restating that precedence. Two copies would be two answers
    to "how does this process authenticate", and the preflight's answer is
    the one an operator would then trust.
    """
    options: dict[str, Any] = {
        "enable_endpoint_discovery": False,
        "connection_timeout": connection_timeout,
    }

    if ca_bundle := os.environ.get("COSMOS_CA_BUNDLE"):
        options["connection_verify"] = ca_bundle
    elif os.environ.get("COSMOS_VERIFY_TLS", "true").lower() == "false":
        options["connection_verify"] = False

    if connection_string := os.environ.get("COSMOS_CONNECTION_STRING"):
        return CosmosClient.from_connection_string(connection_string, **options)

    endpoint = os.environ["COSMOS_ENDPOINT"]
    # Master key (emulator/local) bypasses RBAC; otherwise DefaultAzureCredential's
    # chain covers service-principal env vars now and workload-identity (OIDC) /
    # managed identity later with no code change.
    credential = os.environ.get("COSMOS_KEY") or azure.identity.DefaultAzureCredential()

    return CosmosClient(endpoint, credential=credential, **options)


@functools.lru_cache(maxsize=1)
def get_cosmos_client() -> CosmosClient:
    """Build (once) the CosmosClient every write goes through."""
    return _build_client(_CLIENT_TIMEOUT)


@functools.cache
def get_container(database: str, container: str, partition_key: str) -> ContainerProxy:
    """Return a (cached) container handle, creating database/container if allowed.

    On 403 (AAD data-plane auth can't create resources) falls back to plain
    get-client handles, assuming the resources were pre-provisioned.
    """
    client = get_cosmos_client()
    try:
        db = client.create_database_if_not_exists(id=database)
        return db.create_container_if_not_exists(
            id=container,
            partition_key=PartitionKey(path=partition_key),
        )
    except exceptions.CosmosHttpResponseError as e:
        if e.status_code != 403:
            raise
        # AAD token auth: the data SDK forbids database/container creation
        # (management is control-plane only). Assume the resources were
        # pre-provisioned out-of-band (IaC/CLI). A genuine missing-resource or
        # permission error will still surface on the first item operation.
        db = client.get_database_client(database)
        return db.get_container_client(container)


def preflight(database: str, container: str, *, timeout: float = PROBE_TIMEOUT) -> Check:
    """Report whether this process could write lifecycle events to Cosmos.

    Two failures, config before network, because "COSMOS_ENDPOINT is unset"
    is a different sentence from "the endpoint refused us" and only the
    first is fixable without leaving the terminal.

    Deliberately reads existing handles rather than calling `get_container`:
    that function *creates* the database and container as a side effect, and
    a preflight that provisions infrastructure is not a preflight. It also
    builds its own client rather than reusing the cached one, so the probe
    is capped at `timeout` instead of the writer's ten seconds.
    """
    if not (os.environ.get("COSMOS_CONNECTION_STRING") or os.environ.get("COSMOS_ENDPOINT")):
        return Check.failed(
            "events-backend",
            "EVENTS_BACKEND=cosmos but neither COSMOS_CONNECTION_STRING nor "
            "COSMOS_ENDPOINT is set",
            remediation=(
                "export COSMOS_ENDPOINT (plus COSMOS_KEY or an Azure credential), "
                "or set EVENTS_BACKEND=none to run without telemetry"
            ),
            outcome=Outcome.ENVIRONMENT,
        )
    target = f"cosmos {database}/{container}"
    try:
        client = _build_client(timeout)
        client.get_database_client(database).get_container_client(container).read()
    except Exception as exc:
        # Any SDK, auth, TLS or network failure is one report to the
        # operator. Narrowing this would only add branches that produce the
        # same sentence, and a preflight that raises is a preflight that
        # cannot report the other checks.
        return Check.failed(
            "events-backend",
            f"{target} unreachable: {first_line(str(exc)) or type(exc).__name__}",
            remediation=(
                "check COSMOS_ENDPOINT and credentials, and that the database "
                "and container exist"
            ),
            outcome=Outcome.ENVIRONMENT,
        )
    return Check.ok("events-backend", f"{target} reachable")
