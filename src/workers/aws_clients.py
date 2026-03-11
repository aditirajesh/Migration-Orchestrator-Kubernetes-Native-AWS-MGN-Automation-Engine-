import os

import boto3


def get_mgn_client():
    """
    Returns a boto3 client for AWS Application Migration Service (MGN).

    Credentials are read automatically from environment variables:
      AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN (for STS creds)

    Region is read from AWS_DEFAULT_REGION (defaults to us-east-1 if unset).
    """
    return boto3.client(
        "mgn",
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )


