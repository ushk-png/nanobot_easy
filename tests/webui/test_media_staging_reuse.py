"""Opening a conversation again must not copy its attachments again (review A21)."""

from __future__ import annotations

from pathlib import Path

from nanobot.webui.media_api import sign_or_stage_media_path


def _media_dir(root: Path):
    def provider(channel=None):
        directory = root / channel if channel else root
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    return provider


def _staged_files(root: Path) -> list[Path]:
    return sorted(path for path in (root / "websocket").glob("*") if path.is_file())


def test_staging_the_same_file_again_reuses_the_copy(tmp_path) -> None:
    source = tmp_path / "workspace" / "chart.png"
    source.parent.mkdir()
    source.write_bytes(b"\x89PNG" + b"0" * 1000)
    media_root = tmp_path / "media"
    results = [
        sign_or_stage_media_path(source, secret=b"k" * 32, media_dir=_media_dir(media_root))
        for _ in range(4)
    ]
    assert all(result is not None for result in results)
    assert len(_staged_files(media_root)) == 1
    assert len({result["url"] for result in results}) == 1


def test_a_changed_file_gets_its_own_copy_and_no_partial_files_remain(tmp_path) -> None:
    source = tmp_path / "workspace" / "report.txt"
    source.parent.mkdir()
    media_root = tmp_path / "media"
    source.write_text("one")
    first = sign_or_stage_media_path(source, secret=b"k" * 32, media_dir=_media_dir(media_root))
    source.write_text("two, longer")
    second = sign_or_stage_media_path(source, secret=b"k" * 32, media_dir=_media_dir(media_root))
    assert first["url"] != second["url"]
    assert len(_staged_files(media_root)) == 2
    assert not list((media_root / "websocket").glob(".*.part"))
