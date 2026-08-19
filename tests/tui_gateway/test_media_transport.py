from tui_gateway import server


def test_local_media_reference_is_staged_and_rewritten(monkeypatch, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video-bytes")
    seen = {}

    def register_file(session_id, source_path, *, base_url):
        seen.update(
            session_id=session_id,
            source_path=source_path,
            base_url=base_url,
        )
        return {"url": f"{base_url}/v1/files/{session_id}/abc123.mp4"}

    monkeypatch.setattr("tui_gateway.file_serve.register_file", register_file)
    monkeypatch.setenv("HERMES_FILE_SERVE_URL", "https://gateway.example")

    transformed = server._transform_media_refs(
        f"Your video\nMEDIA:{source}",
        "session-1",
    )

    assert transformed == (
        "Your video\n"
        "MEDIA:https://gateway.example/v1/files/session-1/abc123.mp4"
    )
    assert seen == {
        "session_id": "session-1",
        "source_path": str(source),
        "base_url": "https://gateway.example",
    }
