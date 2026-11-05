import io

import pytest

from app.core.errors import APIError
from app.core.storage import S3Storage


@pytest.mark.parametrize("data,valid", [(b"%PDF-example", True), (b"", False), (b"x" * (10 * 1024 * 1024 + 1), False)])
def test_s3_read_enforces_size_and_closes_stream(data, valid):
    body = io.BytesIO(data)
    class Client:
        def get_object(self, **kwargs):
            assert kwargs == {"Bucket": "private-bucket", "Key": "verified/db/key"}
            return {"Body": body}
    storage = S3Storage(Client(), "private-bucket", 300)
    if valid:
        assert storage.read("verified/db/key") == data
    else:
        with pytest.raises(APIError) as error:
            storage.read("verified/db/key")
        assert error.value.code == "INVALID_STORED_FILE"
    assert body.closed
