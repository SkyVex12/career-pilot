"""boto3 clients, untyped: the stubs cover only some services, and callers use few calls."""

from typing import Any


def boto3_client(service: str, **kwargs: Any) -> Any:
    import boto3

    make: Any = getattr(boto3, "client")  # noqa: B009 - the stubs' overloads want literals
    return make(service, **kwargs)
