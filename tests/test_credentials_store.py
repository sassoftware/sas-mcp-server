# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the shared credentials-file helpers."""
import errno
import json
import os
import stat
import sys
import threading
import time

import pytest

from sas_mcp_server import credentials_store as store


def _files(directory):
    return sorted(f.name for f in directory.iterdir())


def _backups(directory):
    return sorted(directory.glob("credentials.json.corrupt-*"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://viya.example.com", "https://viya.example.com"),
        ("https://viya.example.com/", "https://viya.example.com"),
        ("HTTPS://Viya.Example.COM//", "https://viya.example.com"),
        ("https://viya.example.com:443", "https://viya.example.com"),
        ("http://viya.example.com:80/", "http://viya.example.com"),
        ("https://viya.example.com:8443", "https://viya.example.com:8443"),
        ("https://viya.example.com/sas/", "https://viya.example.com/sas"),
        ("https://viya.example.com/?a=1#frag", "https://viya.example.com"),
        ("  https://viya.example.com  ", "https://viya.example.com"),
        ("https://[::1]:8443/", "https://[::1]:8443"),
        ("viya.example.com/", "viya.example.com"),  # not a URL: only trimmed
        ("https://viya.example.com:notaport", "https://viya.example.com:notaport"),
    ],
)
def test_normalize_endpoint(raw, expected):
    assert store.normalize_endpoint(raw) == expected


def test_normalize_endpoint_keeps_schemes_apart():
    assert store.normalize_endpoint("http://x") != store.normalize_endpoint("https://x")


def test_load_blocks_missing_file_is_empty(tmp_path):
    assert store.load_blocks(tmp_path / "absent.json") == {}


@pytest.mark.parametrize("text", ["{nope", "[1]", '"str"', ""])
def test_load_blocks_rejects_invalid_content(tmp_path, text):
    p = tmp_path / "credentials.json"
    p.write_text(text)
    with pytest.raises(ValueError):
        store.load_blocks(p)


def test_unreadable_file_is_not_treated_as_empty(tmp_path, monkeypatch):
    """A read error must abort the write; it must never wipe the other logins."""
    p = tmp_path / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})
    before = p.read_text()

    def locked(self, *a, **k):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(type(p), "read_text", locked)
    with pytest.raises(PermissionError):
        store.update_block(p, "https://b", {"access-token": "B"})
    with pytest.raises(PermissionError):
        store.remove_login(p, "https://a")
    monkeypatch.undo()
    assert p.read_text() == before


@pytest.mark.parametrize("text", ["{nope", "[1]"])
def test_update_block_sets_corrupt_file_aside(tmp_path, text):
    p = tmp_path / "credentials.json"
    p.write_text(text)
    store.update_block(p, "https://a", {"access-token": "A"})
    assert json.loads(p.read_text()) == {"https://a": {"access-token": "A"}}
    [backup] = _backups(tmp_path)
    assert backup.read_text() == text  # nothing is lost


def test_remove_login_refuses_corrupt_file(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text("{nope")
    with pytest.raises(ValueError):
        store.remove_login(p, "https://a")
    assert p.read_text() == "{nope"


def test_update_block_keeps_other_and_unknown_keys(tmp_path):
    p = tmp_path / "sub" / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})
    p.write_text(json.dumps({**json.loads(p.read_text()), "Default": {"access-token": "D"}, "extra": 1}))
    store.update_block(p, "https://b", {"access-token": "B"})
    store.update_block(p, "https://a", {"access-token": "A2"})
    data = json.loads(p.read_text())
    assert data == {
        "https://a": {"access-token": "A2"},
        "Default": {"access-token": "D"},
        "extra": 1,
        "https://b": {"access-token": "B"},
    }


def test_update_block_leaves_no_temp_files(tmp_path):
    p = tmp_path / "credentials.json"
    store.update_block(p, "k", {"access-token": "T"})
    assert _files(tmp_path) == ["credentials.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_update_block_file_is_owner_only(tmp_path):
    p = tmp_path / "credentials.json"
    store.update_block(p, "k", {"access-token": "T"})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_failed_write_keeps_old_file_and_cleans_up(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    store.update_block(p, "k", {"access-token": "OLD"})

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(store.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.update_block(p, "k", {"access-token": "NEW"})
    assert json.loads(p.read_text())["k"]["access-token"] == "OLD"
    assert _files(tmp_path) == ["credentials.json"]


def test_remove_login_removes_only_that_endpoint(tmp_path):
    p = tmp_path / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})
    store.update_block(p, "https://b", {"access-token": "B"})
    assert store.remove_login(p, "HTTPS://A/") == (["https://a"], False)
    assert store.remove_login(p, "https://a") == ([], False)
    assert json.loads(p.read_text()) == {"https://b": {"access-token": "B"}}
    assert store.remove_login(tmp_path / "absent.json", "https://a") == ([], False)


def test_remove_login_also_removes_default_bound_to_that_endpoint(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({
        "Default": {"access-token": "D", "endpoint": "https://A/"},
        "https://b": {"access-token": "B"},
    }))
    assert store.remove_login(p, "https://a") == (["Default"], False)
    assert json.loads(p.read_text()) == {"https://b": {"access-token": "B"}}


def test_remove_login_keeps_default_bound_to_another_endpoint(tmp_path):
    p = tmp_path / "credentials.json"
    before = {"Default": {"access-token": "D", "endpoint": "https://b"}, "https://a": {"access-token": "A"}}
    p.write_text(json.dumps(before))
    assert store.remove_login(p, "https://a") == (["https://a"], False)
    assert json.loads(p.read_text()) == {"Default": before["Default"]}


def test_remove_login_flags_an_unbound_default_and_keeps_it(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"Default": {"access-token": "D"}}))
    assert store.remove_login(p, "https://a") == ([], True)
    assert json.loads(p.read_text()) == {"Default": {"access-token": "D"}}  # not rewritten
    store.update_block(p, "https://a", {"access-token": "A"})
    assert store.remove_login(p, "https://a") == (["https://a"], True)


def test_update_block_can_skip_windows_hardening(tmp_path, monkeypatch):
    hardened = []
    monkeypatch.setattr(store, "win_harden", hardened.append)
    p = tmp_path / "credentials.json"
    store.update_block(p, "k", {"access-token": "T"})
    store.update_block(p, "k", {"access-token": "T2"}, harden=False)
    # only the first write, and on the temp file before it takes the real name
    assert [h.name.startswith("credentials.json.") and h.name.endswith(".tmp") for h in hardened] == [True]


def test_the_temp_file_is_restricted_before_it_holds_a_token(tmp_path, monkeypatch):
    """On Windows mkstemp's 0600 is ignored: the ACL must be set while the file is still empty."""
    sizes = []
    monkeypatch.setattr(store, "win_harden", lambda f: sizes.append(os.path.getsize(f)))
    store.update_block(tmp_path / "credentials.json", "k", {"access-token": "T"})
    assert sizes == [0]


def test_the_temp_file_is_flushed_to_disk_before_the_rename(tmp_path, monkeypatch):
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(store.os, "fsync", lambda fd: order.append("fsync") or real_fsync(fd))
    monkeypatch.setattr(store.os, "replace", lambda s, d: order.append("replace") or real_replace(s, d))
    store.update_block(tmp_path / "credentials.json", "k", {"access-token": "T"})
    assert order == ["fsync", "replace"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_another_tools_file_keeps_its_permission_bits(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_text("{}")
    os.chmod(p, 0o640)
    store.update_block(p, "Default", {"access-token": "T"}, harden=False)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640


def test_another_tools_file_is_rewritten_in_place_on_windows(tmp_path, monkeypatch):
    """A renamed-in file would replace the ACL that tool or an admin set with the folder's."""
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps({"other": 1}))
    monkeypatch.setattr(store.sys, "platform", "win32")

    def no_temp(*a, **k):
        raise AssertionError("must not replace another tool's file")

    monkeypatch.setattr(store.tempfile, "mkstemp", no_temp)
    store.update_block(p, "Default", {"access-token": "T"}, harden=False)
    assert json.loads(p.read_text()) == {"other": 1, "Default": {"access-token": "T"}}


@pytest.mark.skipif(sys.platform != "win32", reason="the retry only applies on Windows")
def test_replace_rides_out_a_brief_lock(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    real = os.replace
    attempts = []

    def flaky(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("scanner has the file open")
        real(src, dst)

    monkeypatch.setattr(store.os, "replace", flaky)
    monkeypatch.setattr(store.time, "sleep", lambda _s: None)
    store.update_block(p, "k", {"access-token": "T"})
    assert len(attempts) == 3
    assert json.loads(p.read_text())["k"]["access-token"] == "T"


def test_persistent_lock_on_rename_falls_back_to_writing_in_place(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})

    def locked(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(store.os, "replace", locked)
    monkeypatch.setattr(store.time, "sleep", lambda _s: None)
    store.update_block(p, "https://b", {"access-token": "B"})
    assert sorted(json.loads(p.read_text())) == ["https://a", "https://b"]
    assert _files(tmp_path) == ["credentials.json"]  # temp file cleaned up


def test_gives_up_when_the_file_cannot_be_written_in_place_either(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})
    before = p.read_text()

    def locked(src, dst):
        raise PermissionError("locked")

    def read_only(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(store.os, "replace", locked)
    monkeypatch.setattr(store.time, "sleep", lambda _s: None)
    monkeypatch.setattr(type(p), "write_text", read_only)
    with pytest.raises(PermissionError):
        store.update_block(p, "https://b", {"access-token": "B"})
    monkeypatch.undo()
    assert p.read_text() == before
    assert _files(tmp_path) == ["credentials.json"]


@pytest.mark.parametrize("code", [errno.EBUSY, errno.EXDEV, errno.EROFS])
def test_rename_unavailable_falls_back_to_writing_in_place(tmp_path, monkeypatch, code):
    """A bind-mounted file (EBUSY) or a filesystem that cannot rename still gets updated."""
    p = tmp_path / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})

    def no_rename(src, dst):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(store.os, "replace", no_rename)
    store.update_block(p, "https://b", {"access-token": "B"})
    assert sorted(json.loads(p.read_text())) == ["https://a", "https://b"]
    assert _files(tmp_path) == ["credentials.json"]


def test_unwritable_directory_falls_back_to_writing_in_place(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})

    def no_temp(*a, **k):
        raise PermissionError("directory is read-only")

    monkeypatch.setattr(store.tempfile, "mkstemp", no_temp)
    store.update_block(p, "https://b", {"access-token": "B"})
    assert sorted(json.loads(p.read_text())) == ["https://a", "https://b"]


def test_other_write_errors_do_not_truncate_the_file(tmp_path, monkeypatch):
    """A full disk must not turn into an in-place rewrite that leaves a truncated file."""
    p = tmp_path / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})
    before = p.read_text()

    def full(src, dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(store.os, "replace", full)
    with pytest.raises(OSError, match="No space"):
        store.update_block(p, "https://b", {"access-token": "B"})
    assert p.read_text() == before


def test_symlinked_credentials_file_is_written_through(tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    target = managed / "credentials.json"
    store.update_block(target, "https://a", {"access-token": "A"})
    link = tmp_path / "credentials.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    store.update_block(link, "https://b", {"access-token": "B"})
    assert link.is_symlink()  # still a link, not replaced by a regular file
    assert sorted(json.loads(target.read_text())) == ["https://a", "https://b"]


def test_corrupt_symlinked_file_moves_the_target_not_the_link(tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    target = managed / "credentials.json"
    target.write_text("{nope")
    link = tmp_path / "credentials.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    store.update_block(link, "https://a", {"access-token": "A"})
    assert link.is_symlink()
    [backup] = _backups(managed)
    assert backup.read_text() == "{nope"
    assert json.loads(target.read_text()) == {"https://a": {"access-token": "A"}}


def test_utf8_blocks_survive_a_rewrite(tmp_path):
    p = tmp_path / "credentials.json"
    p.write_bytes('{"cli": {"user": "Jörg"}}'.encode())
    store.update_block(p, "https://a", {"access-token": "A"}, harden=False)
    assert json.loads(p.read_text(encoding="utf-8"))["cli"] == {"user": "Jörg"}


def test_a_byte_order_mark_is_not_corruption(tmp_path):
    """Some Windows editors (PowerShell 5.1's Out-File) save UTF-8 with a BOM."""
    p = tmp_path / "credentials.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"https://a": {"access-token": "A"}}).encode())
    store.update_block(p, "https://b", {"access-token": "B"})
    assert sorted(json.loads(p.read_text(encoding="utf-8"))) == ["https://a", "https://b"]
    assert _backups(tmp_path) == []


def test_a_file_in_another_encoding_is_left_alone(tmp_path):
    """It may hold valid logins: refuse the update rather than set the file aside."""
    p = tmp_path / "credentials.json"
    original = '{"Default": {"user": "J\u00f6rg"}}'.encode("cp1252")
    p.write_bytes(original)
    with pytest.raises(ValueError, match="not UTF-8"):
        store.update_block(p, "https://a", {"access-token": "A"})
    assert p.read_bytes() == original
    assert _backups(tmp_path) == []


def test_a_second_corruption_keeps_the_first_backup(tmp_path):
    p = tmp_path / "credentials.json"
    for text in ("{first", "{second"):
        p.write_text(text)
        store.update_block(p, "https://a", {"access-token": "A"})
    assert sorted(b.read_text() for b in _backups(tmp_path)) == ["{first", "{second"]


# --- the lock around read-modify-write ----------------------------------------------


def test_the_lock_is_released_after_an_update(tmp_path):
    p = tmp_path / "credentials.json"
    store.update_block(p, "k", {"access-token": "T"})
    store.remove_login(p, "k")
    assert not (tmp_path / "credentials.json.lock").exists()


def test_a_held_lock_makes_the_update_wait_and_then_give_up(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    store.update_block(p, "https://a", {"access-token": "A"})
    before = p.read_text()
    (tmp_path / "credentials.json.lock").write_text("")
    monkeypatch.setattr(store, "_LOCK_TIMEOUT", 0.2)
    with pytest.raises(TimeoutError):
        store.update_block(p, "https://b", {"access-token": "B"})
    assert p.read_text() == before


def test_a_lock_left_by_a_crashed_process_is_cleared(tmp_path):
    p = tmp_path / "credentials.json"
    lock = tmp_path / "credentials.json.lock"
    lock.write_text("")
    old = time.time() - store._LOCK_STALE_AFTER - 5
    os.utime(lock, (old, old))
    store.update_block(p, "https://a", {"access-token": "A"})
    assert json.loads(p.read_text()) == {"https://a": {"access-token": "A"}}
    assert not lock.exists()


def test_an_unwritable_directory_updates_without_a_lock(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    real_open = os.open

    def no_lock(path, *a, **k):
        if str(path).endswith(".lock"):
            raise PermissionError("read-only directory")
        return real_open(path, *a, **k)

    monkeypatch.setattr(store.os, "open", no_lock)
    store.update_block(p, "https://a", {"access-token": "A"})
    assert json.loads(p.read_text()) == {"https://a": {"access-token": "A"}}


def test_concurrent_updates_keep_every_login(tmp_path):
    """Two servers refreshing at once must not drop each other's new tokens."""
    p = tmp_path / "credentials.json"
    store.update_block(p, "seed", {"access-token": "S"})
    real = store.load_blocks

    def slow_read(path):
        data = real(path)
        time.sleep(0.02)  # widen the window between the read and the write
        return data

    store_load = store.load_blocks
    store.load_blocks = slow_read
    try:
        threads = [
            threading.Thread(target=store.update_block, args=(p, f"https://{i}", {"access-token": str(i)}))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        store.load_blocks = store_load
    assert sorted(json.loads(p.read_text())) == sorted(["seed", *(f"https://{i}" for i in range(8))])


# --- endpoint names -------------------------------------------------------------------


def test_names_are_saved_lower_case_and_normalised(tmp_path):
    p = tmp_path / "endpoints.json"
    assert store.save_name(p, "Zeus", "HTTPS://Viya.Example.com:443/") is None
    assert store.load_names(p) == {"zeus": "https://viya.example.com"}
    assert store.save_name(p, "zeus", "https://other.example.com") == "https://viya.example.com"


def test_unusable_entries_in_the_names_file_are_skipped(tmp_path):
    p = tmp_path / "endpoints.json"
    p.write_text(json.dumps({"ok": "https://a", "no url": "https://b", "num": 5, "nourl": "a.example.com"}))
    assert store.load_names(p) == {"ok": "https://a"}
    p.write_text("{broken")
    assert store.load_names(p) == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://typed.example.com/", ("https://typed.example.com/", "url")),
        ("Prod", ("https://viya-prod.example.com", "name")),
        ("zeus", ("https://viya4-s2.zeus.example.com", "host")),
        ("viya4-s2", ("https://viya4-s2.zeus.example.com", "host")),
        ("viya4-s2.zeus.example.com", ("https://viya4-s2.zeus.example.com", "host")),
    ],
)
def test_resolve_endpoint(value, expected):
    names = {"prod": "https://viya-prod.example.com"}
    known = ["https://viya4-s2.zeus.example.com", "https://viya-prod.example.com"]
    assert store.resolve_endpoint(value, names, known) == expected


@pytest.mark.parametrize("value", ["example", "viya", "zeu", ""])
def test_resolve_endpoint_refuses_unclear_words(value):
    known = ["https://viya4-s2.zeus.example.com", "https://viya-prod.example.com"]
    with pytest.raises(store.EndpointNotFoundError):
        store.resolve_endpoint(value, {}, known)


def test_an_abandoned_lock_is_cleared_before_a_waiting_writer_gives_up():
    """A server killed mid-update must not stop the others saving their tokens."""
    assert store._LOCK_STALE_AFTER < store._LOCK_TIMEOUT


def test_a_writer_waits_out_a_killed_servers_lock_and_saves(tmp_path, monkeypatch):
    p = tmp_path / "credentials.json"
    lock = tmp_path / "credentials.json.lock"
    lock.write_text("")
    clock = {"now": time.time()}
    monkeypatch.setattr(store.time, "time", lambda: clock["now"])
    monkeypatch.setattr(store.time, "monotonic", lambda: clock["now"])  # the wait's deadline too

    def sleep(seconds):
        clock["now"] += 1.0  # each wait moves the lock's age on by a second

    monkeypatch.setattr(store.time, "sleep", sleep)
    store.update_block(p, "https://a", {"access-token": "A"})
    assert json.loads(p.read_text()) == {"https://a": {"access-token": "A"}}
    assert not lock.exists()


def test_another_tools_file_gets_no_lock_file(tmp_path, monkeypatch):
    """The sas-viya CLI does not honour our lock; don't create one in its folder."""
    monkeypatch.setattr(store, "_acquire", lambda lock: pytest.fail(f"locked {lock}"))
    p = tmp_path / "credentials.json"
    p.write_text("{}")
    store.update_block(p, "Default", {"access-token": "T"}, harden=False)
    assert _files(tmp_path) == ["credentials.json"]
