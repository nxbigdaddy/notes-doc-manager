"""Independent offline recovery crash/safety tests, exclusively in pytest temp dirs."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from notes.domain.models import FileEntry, NotesError
from notes.infrastructure.store import Store

STAGES = (
    "prepared", "sidecars_removed", "metadata_installed", "committed",
    "sidecars_removed_unjournaled", "metadata_replaced_unjournaled",
)
ACTIVE_NAMES = ("metadata.db", "metadata.db-wal", "metadata.db-shm", "metadata.db-journal")
PROJECT = Path(__file__).resolve().parents[1]
CHILD = Path(__file__).parent / "helpers" / "recovery_child.py"


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class RecoveryCase:
    base: Path
    data: Path
    documents: Path
    backup_json: Path
    backup_sqlite: Path
    document_hashes: dict[str, str]
    old_files: dict[str, bytes]
    expected_tag: str
    document_id: str


@pytest.fixture
def recovery_case(tmp_path):
    data = tmp_path / "data"
    documents = tmp_path / "documents"
    documents.mkdir()
    first = documents / "中文原文.txt"
    first.write_text("Never overwrite this document.\n中文原文 📝", encoding="utf-8")
    second = documents / "unmanaged.md"
    second.write_bytes(b"# Also preserve unregistered documents\r\n")
    store = Store(data)
    library = store.add_library(documents)
    info = first.stat()
    document = store.apply_scan(
        library.id, (FileEntry(first.name, info.st_size, info.st_mtime_ns // 1_000_000),),
        complete=True,
    )[0]
    desired = store.create_tag("backup-state")
    store.set_document_tags(document.id, (desired.id,))
    store.set_favorite(document.id, True)
    store.set_setting("editor_path", "must-not-be-automatically-trusted.exe")
    backup_json = tmp_path / "candidate.json"
    store.export_backup(backup_json)
    snapshot = store.run_automatic_backup(now=1_789_517_000, force=True)
    backup_sqlite = Path(snapshot["path"])
    assert backup_sqlite.is_file()
    store.delete_tag(desired.id)
    previous = store.create_tag("different-live-state")
    store.set_document_tags(document.id, (previous.id,))
    store.set_favorite(document.id, False)
    store.close()
    old_files = {
        "metadata.db": b"damaged SQLite metadata: preserve this exact forensic original\n",
        "metadata.db-wal": b"recognizable damaged WAL evidence\n",
        "metadata.db-shm": b"recognizable damaged SHM evidence\n",
        "metadata.db-journal": b"recognizable damaged rollback-journal evidence\n",
    }
    for name, content in old_files.items():
        (data / name).write_bytes(content)
    return RecoveryCase(
        tmp_path, data, documents, backup_json, backup_sqlite,
        {path.name: digest(path) for path in documents.iterdir()},
        old_files, desired.name, document.id,
    )


def manager(case, **kwargs):
    from notes.infrastructure.recovery import RecoveryManager
    return RecoveryManager(case.data, **kwargs)


def child(case, action, *, kind="json", stage=None):
    output = case.base / f"child-{action}-{kind}-{stage or 'finish'}.json"
    command = [
        sys.executable, str(CHILD), "--data-dir", str(case.data),
        "--action", action, "--report", str(output),
    ]
    if action == "recover":
        backup = case.backup_json if kind == "json" else case.backup_sqlite
        command.extend(("--backup", str(backup)))
    if stage:
        command.extend(("--crash-stage", stage))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT / "src")
    result = subprocess.run(
        command, cwd=PROJECT, env=env, capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    payload = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    return result.returncode, payload


def assert_documents_unchanged(case):
    assert {path.name: digest(path) for path in case.documents.iterdir()} == case.document_hashes


def assert_originals_protected(case):
    for name, content in case.old_files.items():
        copies = tuple((case.data / "recovery").glob(f"*/old-{name}"))
        assert copies, f"No protection copy for {name}"
        assert any(path.read_bytes() == content for path in copies), name


def active_bytes(case):
    return {name: (case.data / name).read_bytes()
            for name in ACTIVE_NAMES if (case.data / name).exists()}


def assert_new_metadata(case):
    uri = (case.data / "metadata.db").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT name FROM tags ORDER BY name").fetchall() == [(case.expected_tag,)]
        assert connection.execute("SELECT id,is_favorite FROM documents").fetchall() == [(case.document_id, 1)]
        assert connection.execute("SELECT value FROM settings WHERE key='editor_path'").fetchall() == []


@pytest.mark.parametrize("kind", ("json", "sqlite"))
@pytest.mark.parametrize("stage", STAGES)
def test_crash_each_durable_stage_resumes_without_losing_evidence(recovery_case, kind, stage):
    case = recovery_case
    code, report = child(case, "recover", kind=kind, stage=stage)
    assert code == 73, report
    assert report["crashed_at"] == stage
    assert_documents_unchanged(case)
    assert_originals_protected(case)
    code, report = child(case, "resume")
    assert code == 0, report
    status = report["result"]["status"]
    assert status in {"completed", "rolled_back"}, report
    if status == "completed":
        assert_new_metadata(case)
    else:
        assert active_bytes(case) == case.old_files
    assert_originals_protected(case)
    assert_documents_unchanged(case)
    # A second startup must not repeat destructive recovery work.
    before = active_bytes(case)
    code, repeated = child(case, "resume")
    assert code == 0 and repeated["result"]["status"] in {"none", "completed", "rolled_back"}, repeated
    assert active_bytes(case) == before


@pytest.mark.parametrize("stage", ("prepared", "sidecars_removed", "metadata_installed"))
@pytest.mark.parametrize("changed", ("main", "sidecar"))
def test_resume_refuses_unknown_live_bytes_after_preparation(recovery_case, stage, changed):
    case = recovery_case
    code, report = child(case, "recover", stage=stage)
    assert code == 73, report
    name = "metadata.db" if changed == "main" else "metadata.db-wal"
    (case.data / name).write_bytes(b"unknown changed live state; do not replace")
    before = active_bytes(case)
    code, report = child(case, "resume")
    assert code == 0 and report["result"]["status"] == "blocked", report
    assert active_bytes(case) == before
    assert_originals_protected(case)
    assert_documents_unchanged(case)


@pytest.mark.parametrize("changed", ("live", "json", "sqlite"))
def test_confirmation_signatures_reject_changed_input(recovery_case, changed):
    case = recovery_case
    recovery = manager(case)
    backup = case.backup_sqlite if changed == "sqlite" else case.backup_json
    current = recovery.inspect_current()
    candidate = recovery.inspect_backup(backup)
    if changed == "live":
        with (case.data / "metadata.db").open("ab") as stream:
            stream.write(b"modified after inspection")
    elif changed == "json":
        with backup.open("a", encoding="utf-8") as stream:
            stream.write("\n ")
    else:
        with closing(sqlite3.connect(backup)) as connection:
            connection.execute("UPDATE tags SET name='changed backup',name_key='changed backup'")
            connection.commit()
    before = active_bytes(case)
    with pytest.raises(NotesError) as error:
        recovery.recover(backup, confirmed=True, expected_current=current["signature"],
                         expected_backup=candidate["signature"])
    assert error.value.code == "state_changed"
    assert active_bytes(case) == before
    assert_documents_unchanged(case)


@pytest.mark.parametrize("kind", ("trigger", "view"))
def test_sqlite_candidate_with_unexpected_executable_schema_is_rejected(recovery_case, kind):
    case = recovery_case
    with closing(sqlite3.connect(case.backup_sqlite)) as connection:
        if kind == "trigger":
            connection.execute("""CREATE TRIGGER untrusted_trigger AFTER INSERT ON settings
                                  BEGIN DELETE FROM tags; END""")
        else:
            connection.execute("CREATE VIEW untrusted_view AS SELECT * FROM tags")
        connection.commit()
    before = active_bytes(case)
    with pytest.raises(NotesError):
        manager(case).inspect_backup(case.backup_sqlite)
    assert active_bytes(case) == before
    assert_documents_unchanged(case)


def test_future_live_schema_is_never_downgraded_by_recovery(recovery_case):
    case = recovery_case
    for name in ACTIVE_NAMES[1:]:
        (case.data / name).unlink()
    (case.data / "metadata.db").write_bytes(case.backup_sqlite.read_bytes())
    with closing(sqlite3.connect(case.data / "metadata.db")) as connection:
        connection.execute("PRAGMA user_version=999")
        connection.commit()
    recovery = manager(case)
    current = recovery.inspect_current()
    candidate = recovery.inspect_backup(case.backup_json)
    before = active_bytes(case)
    with pytest.raises(NotesError) as error:
        recovery.recover(case.backup_json, confirmed=True, expected_current=current["signature"],
                         expected_backup=candidate["signature"])
    assert error.value.code == "unsupported"
    assert active_bytes(case) == before
    assert_documents_unchanged(case)


def journal_after_prepared(case):
    code, report = child(case, "recover", stage="prepared")
    assert code == 73, report
    journals = tuple((case.data / "recovery").glob("*/journal.json"))
    assert len(journals) == 1
    return journals[0]


def rewrite_journal(path, edit):
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    edit(wrapper["payload"])
    canonical = json.dumps(wrapper["payload"], ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    wrapper["sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(wrapper, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("malicious", ("parent", "absolute", "unknown_stage"))
def test_recomputed_journal_hash_does_not_authorize_unsafe_paths_or_states(recovery_case, malicious):
    case = recovery_case
    journal = journal_after_prepared(case)
    outside = case.base / "outside-user-file.db"
    outside.write_bytes(b"unrelated file must remain intact")
    def mutate(payload):
        if malicious == "unknown_stage":
            payload["stage"] = "unknown_future_state"
        else:
            unsafe = "../outside-user-file.db" if malicious == "parent" else str(outside)
            payload["old"][unsafe] = payload["old"].pop("metadata.db")
    rewrite_journal(journal, mutate)
    before = active_bytes(case)
    code, report = child(case, "resume")
    assert code == 0 and report["result"]["status"] == "blocked", report
    assert active_bytes(case) == before
    assert outside.read_bytes() == b"unrelated file must remain intact"
    assert_documents_unchanged(case)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction protection")
def test_resume_rejects_junction_recovery_session(recovery_case):
    import _winapi
    case = recovery_case
    journal = journal_after_prepared(case)
    original = journal.parent
    preserved = case.base / "preserved-session"
    original.rename(preserved)
    _winapi.CreateJunction(str(preserved), str(original))
    before = active_bytes(case)
    code, report = child(case, "resume")
    assert code == 0 and report["result"]["status"] == "blocked", report
    assert active_bytes(case) == before
    assert (preserved / "old-metadata.db").read_bytes() == case.old_files["metadata.db"]
    assert_documents_unchanged(case)


@pytest.mark.parametrize("stage", ("prepared", "sidecars_removed"))
@pytest.mark.parametrize("new_state", ("missing", "corrupt"))
def test_verified_old_bundle_rolls_back_when_prepared_new_database_is_unusable(
    recovery_case, stage, new_state
):
    case = recovery_case
    code, report = child(case, "recover", stage=stage)
    assert code == 73, report
    candidates = tuple((case.data / "recovery").glob("*/new-metadata.db"))
    assert len(candidates) == 1
    if new_state == "missing":
        candidates[0].unlink()
    else:
        candidates[0].write_bytes(b"corrupted prepared database")
    code, report = child(case, "resume")
    assert code == 0 and report["result"]["status"] == "rolled_back", report
    assert active_bytes(case) == case.old_files
    assert_originals_protected(case)
    assert_documents_unchanged(case)


def test_tampered_protective_original_blocks_recovery_without_touching_live(recovery_case):
    case = recovery_case
    journal = journal_after_prepared(case)
    (journal.parent / "old-metadata.db").write_bytes(b"altered protection copy")
    before = active_bytes(case)
    code, report = child(case, "resume")
    assert code == 0 and report["result"]["status"] == "blocked", report
    assert active_bytes(case) == before
    assert_documents_unchanged(case)


def test_cancellation_at_prepared_boundary_preserves_whole_old_bundle(recovery_case):
    case = recovery_case
    cancel = threading.Event()
    def cancel_prepared(stage):
        if stage == "prepared":
            cancel.set()
    recovery = manager(case, fault_hook=cancel_prepared)
    current = recovery.inspect_current()
    candidate = recovery.inspect_backup(case.backup_json)
    with pytest.raises(NotesError) as error:
        recovery.recover(case.backup_json, confirmed=True,
                         expected_current=current["signature"],
                         expected_backup=candidate["signature"], cancel=cancel)
    assert error.value.code == "cancelled"
    assert active_bytes(case) == case.old_files
    assert_originals_protected(case)
    assert_documents_unchanged(case)
    assert recovery.resume()["status"] in {"none", "rolled_back"}


def test_preparation_disk_full_write_failure_does_not_modify_live_or_user_files(
    recovery_case, monkeypatch
):
    case = recovery_case
    recovery = manager(case)
    current = recovery.inspect_current()
    candidate = recovery.inspect_backup(case.backup_json)
    before = active_bytes(case)
    original_open = Path.open
    injected = []

    class FullDiskWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, _value):
            injected.append(True)
            raise OSError(errno.ENOSPC, "Controlled recovery-only disk-full simulation")

    def faulting_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if any(flag in mode for flag in "wxa") and path.is_relative_to(case.data / "recovery"):
            return FullDiskWriter(stream)
        return stream

    with monkeypatch.context() as context:
        context.setattr(Path, "open", faulting_open)
        with pytest.raises((NotesError, OSError)):
            recovery.recover(
                case.backup_json, confirmed=True, expected_current=current["signature"],
                expected_backup=candidate["signature"],
            )
    assert injected, "The test must reach a real recovery write before injecting failure"
    assert active_bytes(case) == before
    assert_documents_unchanged(case)
    assert case.backup_json.is_file() and case.backup_sqlite.is_file()
