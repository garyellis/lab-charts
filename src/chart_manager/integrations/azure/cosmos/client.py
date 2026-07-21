import functools
import os

import azure.identity
from azure.cosmos import CosmosClient, ContainerProxy, PartitionKey, exceptions


@functools.lru_cache(maxsize=1)
def get_cosmos_client() -> CosmosClient:
    options = {
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


@functools.lru_cache(maxsize=None)
def get_container(database: str, container: str, partition_key: str) -> ContainerProxy:
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
