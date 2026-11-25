"""DingTalk inbound attachments must stay inside the media folder (review A39)."""

from __future__ import annotations

from pathlib import Path

import pytest

import nanobot.channels.dingtalk as module


class _Resp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload, self.content, self.text = status, payload or {}, content, ""

    def json(self):
        return self._payload


class _Http:
    def __init__(self, content: bytes = b"data") -> None:
        self.content = content

    async def post(self, url, json=None, headers=None):
        return _Resp(payload={"downloadUrl": "https://example.invalid/f"})

    async def get(self, url, follow_redirects=False):
        return _Resp(content=self.content)


def _channel(content: bytes):
    channel = module.DingTalkChannel(
        {"enabled": True, "clientId": "x", "clientSecret": "y", "allowFrom": []}, bus=None
    )
    channel._http = _Http(content)
    channel._access_token, channel._token_expiry = "t", 1e18
    return channel


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["relative", "absolute", "windows", "nested"])
async def test_downloaded_file_names_cannot_leave_the_media_folder(tmp_path, monkeypatch, kind) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    hostile = {
        "relative": "../../escaped.txt",
        "absolute": str(outside / "abs_target.txt"),
        "windows": "..\\..\\win.txt",
        "nested": "a/b/../../../c.txt",
    }[kind]
    media = tmp_path / "media"
    monkeypatch.setattr("nanobot.config.paths.get_media_dir", lambda name=None: media / (name or ""))
    channel = _channel(b"PWNED")

    saved = await channel._download_dingtalk_file("code", hostile, "../../user")

    assert saved is not None
    resolved = Path(saved).resolve()
    assert resolved.is_relative_to((media / "dingtalk").resolve())
    assert resolved.read_bytes() == b"PWNED"
    assert not (tmp_path / "escaped.txt").exists()
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_same_file_name_from_two_messages_does_not_overwrite(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_media_dir", lambda name=None: tmp_path / (name or ""))
    channel = _channel(b"first")
    first = await channel._download_dingtalk_file("code", "report.pdf", "user1")
    channel._http = _Http(b"second")
    second = await channel._download_dingtalk_file("code", "report.pdf", "user1")

    assert first != second
    assert Path(first).read_bytes() == b"first" and Path(second).read_bytes() == b"second"
    assert Path(first).name.endswith("_report.pdf")


@pytest.mark.asyncio
async def test_oversized_download_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("nanobot.config.paths.get_media_dir", lambda name=None: tmp_path / (name or ""))
    monkeypatch.setattr(module, "_MAX_DOWNLOAD_BYTES", 10)
    channel = _channel(b"x" * 11)
    assert await channel._download_dingtalk_file("code", "big.bin", "u") is None
