from pathlib import Path

import pytest
import tiktoken

from nanobot.utils import helpers
from nanobot.utils.helpers import _write_text_atomic, split_message, truncate_text_to_tokens


def test_split_message_no_code_blocks_unchanged():
    content = "alpha beta gamma delta"

    assert split_message(content, max_len=12) == ["alpha beta", "gamma delta"]


def test_truncate_text_to_tokens_keeps_text_within_budget():
    text = "hello world " * 100

    result = truncate_text_to_tokens(text, 10_000)

    assert result == text


@pytest.mark.requires_tiktoken_encoding
def test_truncate_text_to_tokens_truncates_over_budget():
    enc = tiktoken.get_encoding("cl100k_base")
    text = "word " * 1_000

    result = truncate_text_to_tokens(text, 50)

    assert result.endswith("\n... (truncated)")
    assert len(enc.encode(result)) <= 50


def test_truncate_text_to_tokens_non_positive_budget_returns_text():
    text = "anything"

    assert truncate_text_to_tokens(text, 0) == text


def test_write_text_atomic_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "pairing.json"
    fsync_calls: list[int] = []
    closed_fds: list[int] = []

    def fake_fsync(fd: int) -> None:
        fsync_calls.append(fd)

    monkeypatch.setattr(helpers.os, "fsync", fake_fsync)
    monkeypatch.setattr(helpers.os, "open", lambda path, flags: 12345)
    monkeypatch.setattr(helpers.os, "close", lambda fd: closed_fds.append(fd))

    _write_text_atomic(target, '{"approved": {}}')

    assert target.read_text(encoding="utf-8") == '{"approved": {}}'
    assert len(fsync_calls) == 2
    assert fsync_calls[0] != 12345
    assert fsync_calls[1] == 12345
    assert closed_fds == [12345]


def test_write_text_atomic_keeps_file_when_directory_fsync_is_unsupported(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "pairing.json"
    fsync_calls: list[int] = []

    def fake_open(path, flags):
        raise OSError("directory fsync unsupported")

    monkeypatch.setattr(helpers.os, "fsync", lambda fd: fsync_calls.append(fd))
    monkeypatch.setattr(helpers.os, "open", fake_open)

    _write_text_atomic(target, '{"pending": {}}')

    assert target.read_text(encoding="utf-8") == '{"pending": {}}'
    assert len(fsync_calls) == 1


def test_a_reused_tool_call_id_gets_its_own_file_with_the_new_output(tmp_path) -> None:
    """Review C4-03: the reference pointed at the OLD output when an id was reused."""
    from pathlib import Path

    from nanobot.utils.helpers import maybe_persist_tool_result

    first = maybe_persist_tool_result(tmp_path, "s", "call_0", "FIRST-RUN " * 400, max_chars=100)
    again = maybe_persist_tool_result(tmp_path, "s", "call_0", "FIRST-RUN " * 400, max_chars=100)
    second = maybe_persist_tool_result(tmp_path, "s", "call_0", "SECOND-RUN " * 400, max_chars=100)
    assert first == again  # the same output keeps the same reference

    def saved(reference: str) -> str:
        import re

        match = re.search(r"(/[^\s\]]*call_0[^\s\]]*\.txt)", str(reference))
        assert match, reference
        return Path(match.group(1)).read_text()

    assert saved(first).startswith("FIRST-RUN")
    assert saved(second).startswith("SECOND-RUN")  # not the old file
    assert saved(first) != saved(second)
