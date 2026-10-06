"""Smoke tests for the docker compose stack: Valkey and ElasticMQ answer from Python.

Postgres is covered by migrations/tests/test_rls.py. Needs `docker compose up -d --wait`.
"""

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any, cast

import boto3
import pytest
import valkey
from mypy_boto3_sqs import SQSClient

pytestmark = pytest.mark.stack

VALKEY_URL = os.environ.get("COPILOT_VALKEY_URL", "redis://localhost:6379/0")
SQS_ENDPOINT = os.environ.get("COPILOT_SQS_ENDPOINT", "http://localhost:9324")


def unreachable(what: str, url: str, e: Exception) -> None:
    pytest.fail(
        f"cannot reach {what} at {url} ({e}). Start the local stack with "
        "`docker compose up -d --wait`, or deselect with -m 'not stack'.",
        pytrace=False,
    )


@pytest.fixture(scope="module")
def vk() -> Iterator[valkey.Valkey]:
    client = valkey.Valkey.from_url(VALKEY_URL, socket_timeout=5)
    try:
        client.ping()
    except valkey.ConnectionError as e:
        unreachable("Valkey", VALKEY_URL, e)
    yield client
    client.close()


@pytest.fixture(scope="module")
def sqs() -> SQSClient:
    client = boto3.client(
        "sqs",
        endpoint_url=SQS_ENDPOINT,
        region_name="us-east-1",
        aws_access_key_id="local",
        aws_secret_access_key="local",
    )
    try:
        client.list_queues()
    except Exception as e:  # botocore raises several connection error types
        unreachable("ElasticMQ", SQS_ENDPOINT, e)
    return client


def test_valkey_set_get_with_ttl(vk: valkey.Valkey) -> None:
    # The sync client is typed as "value or awaitable"; cast to what it returns.
    info = cast(dict[str, Any], vk.info("server"))
    assert str(info["valkey_version"]).startswith("8.")
    # Hash-tagged key, as in ARCHITECTURE.md "Valkey key design".
    key = f"sess:{{{uuid.uuid4()}}}:state"
    try:
        assert vk.set(key, "active", ex=30)
        assert vk.get(key) == b"active"
        assert 0 < cast(int, vk.ttl(key)) <= 30
    finally:
        vk.delete(key)
    assert vk.get(key) is None


def test_jobs_queue_has_dead_letter_queue(sqs: SQSClient) -> None:
    url = sqs.get_queue_url(QueueName="jobs")["QueueUrl"]
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    redrive = json.loads(attrs["RedrivePolicy"])
    assert redrive["deadLetterTargetArn"].endswith(":jobs-dlq")
    assert int(redrive["maxReceiveCount"]) == 5
    sqs.get_queue_url(QueueName="jobs-dlq")


def test_sqs_create_send_receive_delete(sqs: SQSClient) -> None:
    url = sqs.create_queue(QueueName=f"smoke-{uuid.uuid4().hex[:12]}")["QueueUrl"]
    try:
        body = json.dumps({"type": "session.ended", "version": 1, "payload": {}})
        # Fair queues: the user id goes in MessageGroupId on a standard queue.
        sqs.send_message(QueueUrl=url, MessageBody=body, MessageGroupId="user-1")
        messages = sqs.receive_message(
            QueueUrl=url, WaitTimeSeconds=5, MessageSystemAttributeNames=["MessageGroupId"]
        ).get("Messages", [])
        assert len(messages) == 1
        message = messages[0]
        assert message.get("Body") == body
        assert message.get("Attributes", {}).get("MessageGroupId") == "user-1"
        sqs.delete_message(QueueUrl=url, ReceiptHandle=message.get("ReceiptHandle", ""))
        empty = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=1).get("Messages", [])
        assert empty == []
    finally:
        sqs.delete_queue(QueueUrl=url)
