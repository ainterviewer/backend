"""Email attachments download as files, never as pages on our origin.

They are user uploads, and those from before the type allowlist can be
anything -- an HTML file rendered inline would run in the app's origin.
"""

import pytest
from fastapi import HTTPException

from app.api.dashboard.participants import _attachment_download


def test_an_attachment_is_served_as_a_download(tmp_path):
    (tmp_path / "Information sheet æ.pdf").write_bytes(b"%PDF")

    response = _attachment_download(tmp_path, "Information sheet æ.pdf")

    assert response.media_type == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''Information%20sheet%20%C3%A6.pdf" in disposition


@pytest.mark.parametrize("name", ["missing.pdf", "../secret.pdf", "sub/x.pdf"])
def test_only_a_file_in_the_directory_is_served(tmp_path, name):
    (tmp_path.parent / "secret.pdf").write_bytes(b"x")

    with pytest.raises(HTTPException) as caught:
        _attachment_download(tmp_path, name)

    assert caught.value.status_code in {400, 404}
