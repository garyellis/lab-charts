import os

import boto3

def get_dynamodb_resource():
    region = os.environ.get('AWS_REGION', 'us-east-1')
    endpoint = os.environ.get('DYNAMODB_ENDPOINT')

    if endpoint:
        # dynamodb-local ignores credential *values*, but boto3 won't sign
        # a request without some access key, so feed it dummies.
        return boto3.resource(
            "dynamodb",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "local"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "local")
        )

    return boto3.resource("dynamodb", region_name=region)


def get_table(table_name: str, partition_key: str, sort_key: str):
    resource = get_dynamodb_resource()
    client = resource.meta.client

    try:
        table = resource.create_table(
            TableName=table_name,
            KeySchema=[
                {"AttributeName": partition_key, "KeyType": "HASH"},
                {"AttributeName": sort_key, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": partition_key, "AttributeType": "S"},
                {"AttributeName": sort_key, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    except client.exceptions.ResourceInUseException:
        table = resource.Table(table_name)

    table.wait_until_exists()
    return table
