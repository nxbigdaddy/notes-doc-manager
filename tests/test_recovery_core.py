"""Recovery verifier/unit boundaries; no real appdata or user document input."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from notes.domain.models import FileEntry, NotesError
from notes.infrastructure.recovery import RecoveryManager, _canonical
from notes.infrastructure.store import Store


@pytest.fixture
def example(tmp_path):
    root = tmp_path / "documents"
    root.mkdir()
    document_path = root / "note.txt"
    document_path.write_text("leave original bytes alone", encoding="utf-8")
    store = Store(tmp_path / "data")
    library = store.add_library(root)
    info = document_path.stat()
    document = store.apply_scan(library.id, (FileEntry("note.txt", info.st_size, info.st_mtime_ns // 1_000_000),), True)[0]
    tag = store.create_tag("desired")
    store.set_document_tags(document.id, (tag.id,))
    store.set_favorite(document.id, True)
    store.set_setting("editor_path", "never-activate.exe")
    backup = tmp_path / "saved.json"
    store.export_backup(backup)
    return store, RecoveryManager(store.data_dir), root, document, backup


def corrupt(store):
    store.metadata_path.write_bytes(b"damaged original bytes: retain forever")


def read_tags(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT name FROM tags ORDER BY name").fetchall()


def test_missing_inspection_and_lexical_validator_do_not_create_or_connect(tmp_path, monkeypatch):
    absent = tmp_path / "absent"
    payload = {"format_version": 1, "libraries": [], "documents": [], "tags": [],
               "document_tags": [], "settings": []}
    manager = RecoveryManager(absent)
    def forbidden(*args, **kwargs):
        raise AssertionError("no initialization I/O allowed")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    assert manager.inspect_current()["status"] == "missing"
    Store.validate_backup_payload(payload, absent, check_paths=False)
    assert not absent.exists()


def test_healthy_inspection_does_not_modify_live_bundle(example):
    store, manager, _, _, _ = example
    before = {p.name: p.read_bytes() for p in store.data_dir.glob("metadata.db*")}
    result = manager.inspect_current()
    assert result["status"] == "healthy" and result["version"] == 1
    assert result["signature"] == result["fingerprint"]
    assert {p.name: p.read_bytes() for p in store.data_dir.glob("metadata.db*")} == before


def test_existing_empty_or_missing_schema_is_corrupt_not_silently_initialized(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "metadata.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version=1")
    before = path.read_bytes()
    assert RecoveryManager(data).inspect_current()["status"] == "corrupt"
    assert path.read_bytes() == before


@pytest.mark.parametrize("history", ["search.db", "search-v2.db", "search-v2.db-wal", "backups", "recovery"])
def test_missing_main_with_history_does_not_initialize_empty_metadata(tmp_path, history):
    data = tmp_path / "data"
    data.mkdir()
    item = data / history
    if history in {"backups", "recovery"}:
        item.mkdir()
        (item / "evidence.txt").write_bytes(b"preserve old evidence")
    else:
        item.write_bytes(b"preserve cache evidence")
    before = {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}
    state = RecoveryManager(data).inspect_current()
    assert state["status"] == "corrupt" and state["recoverable"]
    assert not (data / "metadata.db").exists()
    assert {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()} == before


def test_logs_only_is_still_a_new_profile(tmp_path):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "logs" / "notes.log").write_text("startup", encoding="utf-8")
    (data / "instance.lock").write_bytes(b"")
    assert RecoveryManager(data).inspect_current()["status"] == "missing"
    assert not (data / "metadata.db").exists()


def test_history_permission_failure_is_not_treated_as_absent(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    manager = RecoveryManager(data)
    original = manager._plain_chain
    def inaccessible(path, *args, **kwargs):
        if path == data / "search.db":
            raise PermissionError("fixture denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(manager, "_plain_chain", inaccessible)
    assert manager.inspect_current()["status"] == "inaccessible"
    assert not (data / "metadata.db").exists()


def test_unsupported_volume_is_not_mislabeled_recoverable_corruption(tmp_path, monkeypatch):
    manager = RecoveryManager(tmp_path / "data")
    def unsupported(_path):
        raise NotesError("unsupported_volume", "unsupported fixture volume")
    monkeypatch.setattr(manager, "_local_volume", unsupported)
    result = manager.inspect_current()
    assert result["status"] == "unsupported" and not result["recoverable"]


def test_future_header_is_rejected_even_if_remaining_database_is_damaged(example):
    store, manager, _, _, backup = example
    raw = bytearray(store.metadata_path.read_bytes()[:100])
    raw[60:64] = (99).to_bytes(4, "big")
    store.metadata_path.write_bytes(raw)
    state = manager.inspect_current()
    assert state["status"] == "unsupported" and state["version"] == 99
    candidate = manager.inspect_backup(backup)
    with pytest.raises(NotesError) as error:
        manager.recover(backup, True, state["signature"], candidate["signature"])
    assert error.value.code == "unsupported"
    assert store.metadata_path.read_bytes() == raw


def test_offline_or_mapped_volume_library_is_not_probed_during_validation(example, monkeypatch):
    store, manager, _, _, backup = example
    payload = json.loads(backup.read_text(encoding="utf-8"))
    payload["libraries"][0]["root_path"] = "Z:\\untrusted-network-reference"
    backup.write_text(json.dumps(payload), encoding="utf-8")
    local = manager._local_volume
    plain = manager._plain_chain
    def volume(path):
        if path.drive.upper() == "Z:":
            raise NotesError("unsupported_volume", "not a local fixed volume")
        return local(path)
    def no_network_probe(path, *args, **kwargs):
        assert path.drive.upper() != "Z:", "never stat a mapped/unsupported root"
        return plain(path, *args, **kwargs)
    monkeypatch.setattr(manager, "_local_volume", volume)
    monkeypatch.setattr(manager, "_plain_chain", no_network_probe)
    candidate = manager.inspect_backup(backup)
    corrupt(store)
    current = manager.inspect_current()
    result = manager.recover(backup, True, current["signature"], candidate["signature"])
    assert result["recovered"]
    with closing(sqlite3.connect(store.metadata_path)) as connection:
        assert connection.execute("SELECT root_path,availability FROM libraries").fetchone() == (
            "Z:\\untrusted-network-reference", "unknown")


def test_local_alias_cannot_bypass_real_appdata_boundary(example, tmp_path, monkeypatch):
    store, manager, _, _, backup = example
    alias = tmp_path / "local-alias"
    alias.mkdir()
    payload = json.loads(backup.read_text(encoding="utf-8"))
    payload["libraries"][0]["root_path"] = str(alias)
    backup.write_text(json.dumps(payload), encoding="utf-8")
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        if path == alias:
            return original(store.data_dir, *args, **kwargs)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(NotesError) as error:
        manager.inspect_backup(backup)
    assert error.value.code == "invalid_backup"


def test_candidate_committed_wal_is_read_and_original_bundle_unchanged(example, tmp_path):
    store, manager, root, _, _ = example
    source = tmp_path / "writer.db"
    source.write_bytes(store.metadata_path.read_bytes())
    captured = tmp_path / "captured"
    captured.mkdir()
    candidate = captured / "snapshot.db"
    with closing(sqlite3.connect(source)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE tags SET name='committed-in-wal',name_key='committed-in-wal'")
        writer.commit()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(source) + suffix)
            if path.exists():
                Path(str(candidate) + suffix).write_bytes(path.read_bytes())
    before = {p.name: p.read_bytes() for p in captured.iterdir()}
    info = manager.inspect_backup(candidate)
    assert {p.name: p.read_bytes() for p in captured.iterdir()} == before
    content = (root / "note.txt").read_bytes()
    corrupt(store)
    current = manager.inspect_current()
    assert manager.recover(candidate, True, current["signature"], info["signature"])["recovered"]
    assert read_tags(store.metadata_path) == [("committed-in-wal",)]
    assert (root / "note.txt").read_bytes() == content


@pytest.mark.parametrize("kind", ["generated", "expression_default", "expression_index"])
def test_sqlite_expressions_are_rejected_before_import(example, tmp_path, kind):
    store, manager, _, _, _ = example
    candidate = tmp_path / "candidate.db"
    candidate.write_bytes(store.metadata_path.read_bytes())
    with closing(sqlite3.connect(candidate)) as connection:
        if kind == "generated":
            connection.execute("ALTER TABLE tags ADD COLUMN evil TEXT GENERATED ALWAYS AS (name||name) VIRTUAL")
        elif kind == "expression_default":
            connection.execute("DROP TABLE settings")
            connection.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT DEFAULT (zeroblob(1073741824)))")
        else:
            connection.execute("CREATE INDEX evil_index ON tags(lower(name))")
        connection.commit()
    with pytest.raises(NotesError):
        manager.inspect_backup(candidate)


@pytest.mark.parametrize("value", [1, "true", None])
def test_only_explicit_boolean_confirmation_can_recover(example, value):
    store, manager, _, _, backup = example
    corrupt(store)
    state = manager.inspect_current()
    candidate = manager.inspect_backup(backup)
    before = store.metadata_path.read_bytes()
    with pytest.raises(NotesError) as failure:
        manager.recover(backup, value, state["signature"], candidate["signature"])
    assert failure.value.code == "confirmation_required"
    assert store.metadata_path.read_bytes() == before


def test_cancel_before_commit_never_installs_new_metadata(example):
    store, manager, _, _, backup = example
    corrupt(store)
    state = manager.inspect_current()
    candidate = manager.inspect_backup(backup)
    cancellation = threading.Event()
    manager.fault_hook = lambda stage: cancellation.set() if stage == "prepared" else None
    with pytest.raises(NotesError) as failure:
        manager.recover(backup, True, state["signature"], candidate["signature"], cancellation)
    assert failure.value.code == "cancelled"
    assert store.metadata_path.read_bytes() == b"damaged original bytes: retain forever"
    assert manager.resume()["status"] == "none"


def test_resigned_journal_cannot_activate_trigger_or_editor_settings(example):
    store, manager, _, _, backup = example
    corrupt(store)
    state = manager.inspect_current()
    candidate = manager.inspect_backup(backup)
    class SimulatedCrash(BaseException):
        pass
    def crash(stage):
        if stage == "prepared":
            raise SimulatedCrash()
    manager.fault_hook = crash
    with pytest.raises(SimulatedCrash):
        manager.recover(backup, True, state["signature"], candidate["signature"])
    session = next((store.data_dir / "recovery").iterdir())
    new = session / "new-metadata.db"
    with closing(sqlite3.connect(new)) as connection:
        connection.execute("INSERT INTO settings VALUES ('editor_path','untrusted.exe')")
        connection.execute("""CREATE TRIGGER injected AFTER INSERT ON tags
            BEGIN DELETE FROM documents; END""")
        connection.commit()
    journal_path = session / "journal.json"
    envelope = json.loads(journal_path.read_text(encoding="utf-8"))
    envelope["payload"]["new"] = {"size": new.stat().st_size,
                                  "sha256": hashlib.sha256(new.read_bytes()).hexdigest()}
    envelope["sha256"] = hashlib.sha256(_canonical(envelope["payload"])).hexdigest()
    journal_path.write_text(json.dumps(envelope), encoding="utf-8")
    result = manager.resume()
    assert result["status"] in {"rolled_back", "blocked"}
    assert store.metadata_path.read_bytes() == b"damaged original bytes: retain forever"


def test_completion_history_does_not_block_later_legitimate_metadata_changes(example):
    store, manager, _, _, backup = example
    corrupt(store)
    state = manager.inspect_current()
    candidate = manager.inspect_backup(backup)
    manager.recover(backup, True, state["signature"], candidate["signature"])
    reopened = Store(store.data_dir)
    reopened.create_tag("legitimate later edit")
    assert manager.resume()["status"] == "none"
    assert manager.inspect_current()["status"] == "healthy"
