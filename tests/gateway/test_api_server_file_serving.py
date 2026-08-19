from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.fixture
def adapter():
    return APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-test"})
    )


def request(*, session_id="session-1", filename="file.mp4", token="sk-test"):
    value = MagicMock()
    value.headers = {"Authorization": f"Bearer {token}"} if token else {}
    value.match_info = {"session_id": session_id, "filename": filename}
    return value


def test_file_route_is_registered(adapter):
    assert (
        "GET",
        "/v1/files/{session_id}/{filename}",
        adapter._handle_file_download,
    ) in adapter._http_route_table()


@pytest.mark.asyncio
async def test_file_download_requires_auth(adapter):
    response = await adapter._handle_file_download(request(token="wrong"))
    assert response.status == 401


@pytest.mark.asyncio
async def test_file_download_returns_404_for_unknown_file(adapter, monkeypatch):
    monkeypatch.setattr("tui_gateway.file_serve.resolve_file", lambda *_: None)
    response = await adapter._handle_file_download(request())
    assert response.status == 404
    assert response.text == "File not found."


@pytest.mark.asyncio
async def test_file_download_returns_file_response(adapter, monkeypatch, tmp_path):
    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"video-bytes")
    monkeypatch.setattr("tui_gateway.file_serve.resolve_file", lambda *_: source)

    response = await adapter._handle_file_download(request())

    assert response.status == 200
    assert Path(response._path) == source
    assert response.headers["Content-Disposition"] == 'inline; filename="fixture.mp4"'
    assert response.headers["Cache-Control"] == "private, max-age=300"
