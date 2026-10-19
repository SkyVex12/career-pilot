"""The S3 store against Cloud Storage's XML API: no multi-object delete, no checksum trailers."""

from typing import Any

from copilot_core.config import DataSettings
from copilot_core.storage import S3Store, is_gcs


class FakeS3:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys
        self.calls: list[str] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        return {"Contents": [{"Key": k} for k in self.keys if k.startswith(kwargs["Prefix"])]}

    def delete_object(self, Bucket: str, Key: str) -> None:
        self.calls.append(f"delete_object {Key}")

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> None:
        self.calls.append(f"delete_objects {len(Delete['Objects'])}")


def settings(endpoint: str | None) -> DataSettings:
    return DataSettings(_env_file=None, env="test", s3_endpoint=endpoint)  # pyright: ignore[reportCallIssue]


async def test_cloud_storage_deletes_a_prefix_one_object_at_a_time() -> None:
    keys = ["uploads/u1/documents/a.pdf", "uploads/u1/me-audio/s1", "uploads/u2/x"]
    fake = FakeS3(keys)
    store = S3Store(settings("https://storage.googleapis.com"), client=fake)
    assert await store.delete_prefix("uploads/u1/") == 2
    assert fake.calls == [
        "delete_object uploads/u1/documents/a.pdf",
        "delete_object uploads/u1/me-audio/s1",
    ]
    # S3 and the local store keep the batch delete.
    fake = FakeS3(keys)
    assert (
        await S3Store(settings("http://localhost:8333"), client=fake).delete_prefix("uploads/") == 3
    )
    assert fake.calls == ["delete_objects 3"]


def test_is_gcs() -> None:
    assert is_gcs("https://storage.googleapis.com")
    assert not is_gcs(None)
    assert not is_gcs("")
    assert not is_gcs("http://localhost:8333")


def test_the_client_signs_for_the_s3_region_and_skips_optional_checksums() -> None:
    s = DataSettings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        env="test",
        s3_endpoint="https://storage.googleapis.com",
        s3_region="auto",
        s3_access_key="GOOG1Etest",
        s3_secret_key="secret",
    )
    client: Any = S3Store(s).client
    assert client.meta.region_name == "auto"
    assert client.meta.config.request_checksum_calculation == "when_required"
    url = client.generate_presigned_url(
        "put_object", Params={"Bucket": "b", "Key": "uploads/u/k", "ContentType": "text/plain"}
    )
    assert url.startswith("https://storage.googleapis.com/b/uploads/u/k?")
    assert "%2Fauto%2Fs3%2Faws4_request" in url
    assert "x-amz-checksum" not in url.lower()

