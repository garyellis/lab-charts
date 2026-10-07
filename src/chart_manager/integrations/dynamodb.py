"""DynamoDB access via boto3, with a DYNAMODB_ENDPOINT override for dynamodb-local."""

import os
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config

from chart_manager.plumbing.exit_codes import Outcome
from chart_manager.plumbing.preflight import PROBE_TIMEOUT, Check, first_line

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table


def _session_kwargs() -> dict[str, Any]:
    """Region, endpoint and credentials for one boto3 handle.

    Shared by the resource every write goes through and by `preflight`, so
    the probe cannot end up asking a different account or a different
    endpoint than the writer -- which would make a green preflight say
    nothing about whether events actually land.
    """
    region = os.environ.get("AWS_REGION", "us-east-1")
    endpoint = os.environ.get("DYNAMODB_ENDPOINT")
    if endpoint:
        # dynamodb-local ignores credential *values*, but boto3 won't sign
        # a request without some access key, so feed it dummies.
        return {
            "endpoint_url": endpoint,
            "region_name": region,
            "aws_access_key_id": os.environ.get("AWS_ACCESS_KEY_ID", "local"),
            "aws_secret_access_key": os.environ.get("AWS_SECRET_ACCESS_KEY", "local"),
        }
    return {"region_name": region}


def get_dynamodb_resource():
    """Build a boto3 DynamoDB resource; DYNAMODB_ENDPOINT switches to local mode."""
    return boto3.resource("dynamodb", **_session_kwargs())


class DynamoDBTable:
    """One DynamoDB table, written as JSON documents."""

    def __init__(self, table: "Table") -> None:
        """Bind the boto3 Table."""
        self._table = table

    def put(self, item: dict[str, Any]) -> None:
        """Write `item`, replacing any item with the same key."""
        self._table.put_item(Item=item)


def get_table(table_name: str) -> DynamoDBTable:
    """Return a handle on an existing table; nothing is created or checked."""
    return DynamoDBTable(get_dynamodb_resource().Table(table_name))


def preflight(table_name: str, *, timeout: float = PROBE_TIMEOUT) -> Check:
    """Report whether the lifecycle-events table is reachable and present.

    `get_table` is a lazy handle that never touches the network, so this asks
    `describe_table` instead: one read-only question with retries disabled and
    both socket timeouts capped, because an unreachable endpoint is precisely
    the condition being probed.
    """
    where = os.environ.get("DYNAMODB_ENDPOINT") or (
        f"region {os.environ.get('AWS_REGION', 'us-east-1')}"
    )
    target = f"dynamodb table {table_name}"
    config = Config(
        connect_timeout=timeout,
        read_timeout=timeout,
        retries={"max_attempts": 1},
    )
    try:
        client = boto3.client("dynamodb", config=config, **_session_kwargs())
        client.describe_table(TableName=table_name)
    except Exception as exc:
        # Missing table, missing credentials, wrong region and an unreachable
        # endpoint all arrive as different botocore exception types and all
        # mean the same thing to the operator: events will not be written.
        return Check.failed(
            "events-backend",
            f"{target} unavailable at {where}: {first_line(str(exc)) or type(exc).__name__}",
            remediation=(
                "check AWS credentials and AWS_REGION (or DYNAMODB_ENDPOINT); "
                "create a missing table with `mise run events:provision`"
            ),
            outcome=Outcome.ENVIRONMENT,
        )
    return Check.ok("events-backend", f"{target} reachable at {where}")
