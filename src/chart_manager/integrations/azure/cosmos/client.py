"""Cosmos DB access: cached client and container handles, env-var configured."""

import functools
import os
from typing import Any

import azure.identity
from azure.cosmos import ContainerProxy, CosmosClient, PartitionKey, exceptions


@functools.lru_cache(maxsize=1)
def get_cosmos_client() -> CosmosClient:
    """Build (once) the CosmosClient.

    Auth precedence: COSMOS_CONNECTION_STRING > COSMOS_KEY > DefaultAzureCredential
    against COSMOS_ENDPOINT. COSMOS_CA_BUNDLE / COSMOS_VERIFY_TLS tune TLS
    verification for the emulator.
    """
    options: dict[str, Any] = {
        "enable_endpoint_discovery": False,
        "connection_timeout": 10,
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
