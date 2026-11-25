import pytest

from nanobot.pairing import __all__ as pairing_all
from nanobot.pairing import store


def test_all_exports_are_importable():
    """Every name in __all__ must actually be importable from nanobot.pairing."""
    import nanobot.pairing as pkg

    for name in pairing_all:
        assert hasattr(pkg, name), f"{name} is in __all__ but not exported"


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    path = tmp_path / "pairing.json"
    monkeypatch.setattr(store, "_store_path", lambda: path)


class TestGenerateCode:
    def test_format(self) -> None:
        code = store.generate_code("telegram", "123")
        assert len(code) == 9  # 4 + 1 + 4
        assert code[4] == "-"
        assert code.replace("-", "").isalnum()
        assert code.replace("-", "").isupper()

    def test_uniqueness(self) -> None:
        codes = {store.generate_code("telegram", str(i)) for i in range(20)}
        assert len(codes) == 20

    def test_ttl_expiration(self, monkeypatch) -> None:
        clock = {"now": 1_000.0}
        monkeypatch.setattr(store.time, "time", lambda: clock["now"])

        code = store.generate_code("telegram", "123", ttl=1)
        assert store.approve_code(code) == ("telegram", "123")

        code2 = store.generate_code("telegram", "456", ttl=0)
        clock["now"] += 0.1
        assert store.approve_code(code2) is None


class TestApproveDeny:
    def test_approve_moves_to_approved(self) -> None:
        code = store.generate_code("telegram", "123")
        assert store.is_approved("telegram", "123") is False

        result = store.approve_code(code)
        assert result == ("telegram", "123")
        assert store.is_approved("telegram", "123") is True
        assert store.get_approved("telegram") == ["123"]

    def test_deny_removes_pending(self) -> None:
        code = store.generate_code("telegram", "123")
        assert store.deny_code(code) is True
        assert store.approve_code(code) is None

    def test_deny_unknown_returns_false(self) -> None:
        assert store.deny_code("UNKNOWN") is False

    def test_approve_expired_returns_none(self, monkeypatch) -> None:
        clock = {"now": 1_000.0}
        monkeypatch.setattr(store.time, "time", lambda: clock["now"])

        code = store.generate_code("telegram", "123", ttl=0)
        clock["now"] += 0.1
        assert store.approve_code(code) is None


class TestRevoke:
    def test_revoke_removes_sender(self) -> None:
        code = store.generate_code("telegram", "123")
        store.approve_code(code)
        assert store.is_approved("telegram", "123") is True

        assert store.revoke("telegram", "123") is True
        assert store.is_approved("telegram", "123") is False
        assert store.get_approved("telegram") == []

    def test_revoke_unknown_returns_false(self) -> None:
        assert store.revoke("telegram", "999") is False


class TestListPending:
    def test_empty(self) -> None:
        assert store.list_pending() == []

    def test_shows_pending(self) -> None:
        store.generate_code("telegram", "123")
        store.generate_code("discord", "456")
        pending = store.list_pending()
        assert len(pending) == 2
        channels = {p["channel"] for p in pending}
        assert channels == {"telegram", "discord"}

    def test_expired_not_listed(self, monkeypatch) -> None:
        clock = {"now": 1_000.0}
        monkeypatch.setattr(store.time, "time", lambda: clock["now"])

        store.generate_code("telegram", "123", ttl=0)
        clock["now"] += 0.1
        assert store.list_pending() == []


class TestHandlePairingCommand:
    def test_list_empty(self) -> None:
        reply = store.handle_pairing_command("telegram", "list")
        assert reply == "No pending pairing requests."

    def test_list_pending(self) -> None:
        store.generate_code("telegram", "123")
        reply = store.handle_pairing_command("telegram", "list")
        assert "Pending pairing requests:" in reply
        assert "telegram" in reply
        assert "123" in reply

    def test_approve(self) -> None:
        code = store.generate_code("telegram", "123")
        reply = store.handle_pairing_command("telegram", f"approve {code}")
        assert "Approved" in reply
        assert "123" in reply
        assert store.is_approved("telegram", "123") is True

    def test_approve_invalid(self) -> None:
        reply = store.handle_pairing_command("telegram", "approve BAD-CODE")
        assert "Invalid or expired" in reply

    def test_approve_no_arg(self) -> None:
        reply = store.handle_pairing_command("telegram", "approve")
        assert "Usage:" in reply

    def test_deny(self) -> None:
        code = store.generate_code("telegram", "123")
        reply = store.handle_pairing_command("telegram", f"deny {code}")
        assert "Denied" in reply
        assert store.approve_code(code) is None

    def test_deny_unknown(self) -> None:
        reply = store.handle_pairing_command("telegram", "deny BAD-CODE")
        assert "not found" in reply

    def test_revoke_current_channel(self) -> None:
        code = store.generate_code("telegram", "123")
        store.approve_code(code)
        reply = store.handle_pairing_command("telegram", "revoke 123")
        assert "Revoked" in reply
        assert store.is_approved("telegram", "123") is False

    def test_revoke_other_channel(self) -> None:
        code = store.generate_code("discord", "456")
        store.approve_code(code)
        # Two-arg form: first arg is channel, second is user
        reply = store.handle_pairing_command("telegram", "revoke discord 456")
        assert "Revoked" in reply
        assert store.is_approved("discord", "456") is False

    def test_revoke_unknown(self) -> None:
        reply = store.handle_pairing_command("telegram", "revoke 999")
        assert "was not in the approved list" in reply

    def test_revoke_no_arg(self) -> None:
        reply = store.handle_pairing_command("telegram", "revoke")
        assert "Usage:" in reply

    def test_unknown_subcommand(self) -> None:
        reply = store.handle_pairing_command("telegram", "foo")
        assert "Unknown pairing command" in reply

    def test_default_to_list(self) -> None:
        store.generate_code("telegram", "123")
        reply = store.handle_pairing_command("telegram", "")
        assert "Pending pairing requests:" in reply


class TestNonStringSenderId:
    def test_numeric_sender_id_round_trip(self) -> None:
        code = store.generate_code("telegram", 12345)
        assert store.approve_code(code) == ("telegram", "12345")
        assert store.is_approved("telegram", 12345) is True
        assert store.is_approved("telegram", "12345") is True
        assert store.get_approved("telegram") == ["12345"]
        assert store.revoke("telegram", 12345) is True
        assert store.is_approved("telegram", "12345") is False

    def test_hand_edited_numeric_pending_does_not_corrupt_approved_set(self) -> None:
        store._store_path().write_text(
            '{"approved": {"telegram": ["111"]}, '
            '"pending": {"ABCD-EFGH": {"channel": "telegram", "sender_id": 222, '
            '"created_at": 1000.0, "expires_at": 9999999999.0}}}',
            encoding="utf-8",
        )
        assert store.approve_code("ABCD-EFGH") == ("telegram", "222")
        assert store.is_approved("telegram", 222) is True
        store.generate_code("telegram", 333)
        assert store.get_approved("telegram") == ["111", "222"]

    def test_numeric_id_in_hand_edited_store(self) -> None:
        store._store_path().write_text(
            '{"approved": {"telegram": [12345]}, "pending": {}}',
            encoding="utf-8",
        )
        assert store.is_approved("telegram", "12345") is True
        assert store.is_approved("telegram", 12345) is True
        assert store.revoke("telegram", 12345) is True
        assert store.is_approved("telegram", "12345") is False


class TestStoreDurability:
    def test_corruption_recovery(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "pairing.json"
        path.write_text("not json{", encoding="utf-8")
        monkeypatch.setattr(store, "_store_path", lambda: path)

        # Should recover gracefully and act as empty store
        assert store.list_pending() == []
        assert store.is_approved("telegram", "123") is False


class TestReadFailuresNeverWipeApprovals:
    """Review N17: one transient read error used to replace the store with an empty one."""

    def _seed(self) -> None:
        import json

        store._store_path().write_text(
            json.dumps({"approved": {"telegram": ["111", "222"]}, "pending": {}}), encoding="utf-8"
        )

    def test_transient_read_error_does_not_reset_approved_senders(self, monkeypatch) -> None:
        import builtins
        import errno
        import json

        self._seed()
        real_open = builtins.open
        failures = {"left": 1}

        def flaky(path, *args, **kwargs):
            if str(path).endswith("pairing.json") and failures["left"]:
                failures["left"] -= 1
                raise OSError(errno.EMFILE, "Too many open files")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", flaky)
        with pytest.raises(OSError):
            store.generate_code("telegram", "999")  # a stranger's DM during the failure
        monkeypatch.setattr(builtins, "open", real_open)

        assert store.is_approved("telegram", "111")
        assert json.loads(store._store_path().read_text())["approved"] == {
            "telegram": ["111", "222"]
        }

    def test_reader_sees_empty_store_for_one_call_without_writing(self, monkeypatch) -> None:
        import builtins
        import errno

        self._seed()
        before = store._store_path().read_text()
        real_open = builtins.open

        def always_fail(path, *args, **kwargs):
            if str(path).endswith("pairing.json"):
                raise OSError(errno.EIO, "I/O error")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", always_fail)
        assert store.is_approved("telegram", "111") is False
        monkeypatch.setattr(builtins, "open", real_open)
        assert store._store_path().read_text() == before

    def test_invalid_json_is_moved_aside_not_overwritten(self) -> None:
        path = store._store_path()
        path.write_text("{not json", encoding="utf-8")
        assert store.is_approved("telegram", "111") is False
        store.generate_code("telegram", "5")
        saved = list(path.parent.glob("pairing.json.corrupt-*"))
        assert len(saved) == 1 and saved[0].read_text() == "{not json"

    def test_non_object_json_is_treated_as_corrupt(self) -> None:
        path = store._store_path()
        path.write_text("[1, 2]", encoding="utf-8")
        assert store.get_approved("telegram") == []
        assert list(path.parent.glob("pairing.json.corrupt-*"))
