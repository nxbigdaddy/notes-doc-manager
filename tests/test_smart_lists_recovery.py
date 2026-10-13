"""Smart-list recovery is logical, versioned, strict, and document-byte preserving."""

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest

from notes.domain.models import FileEntry, NotesError
from notes.domain.smart_lists import decode_query, encode_query, query_payload
from notes.infrastructure.recovery import RecoveryManager
from notes.infrastructure.store import Store, normalize_text

PROJECT = Path(__file__).resolve().parents[1]
CHILD = Path(__file__).parent / "helpers" / "recovery_child.py"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Sample:
    base: Path
    source_data: Path
    target_data: Path
    document: Path
    document_hash: str
    json_backup: Path
    sqlite_backup: Path
    smart_rows: list[dict]
    library_id: str
    tag_id: str
    old_target: bytes

    def candidate(self, kind):
        return self.json_backup if kind == "json" else self.sqlite_backup


def make_sample(tmp_path, *, version=2, dangling=None):
    from notes.infrastructure.store import SMART_LIST_SCHEMA
    documents = tmp_path / "documents"
    (documents / "nested").mkdir(parents=True)
    document_path = documents / "nested" / "中文.txt"
    document_path.write_text("Original document bytes must remain unchanged.\n中文 📝", encoding="utf-8")
    source = Store(tmp_path / "source-data")
    library = source.add_library(documents)
    info = document_path.stat()
    document = source.apply_scan(
        library.id,
        (FileEntry("nested/中文.txt", info.st_size, info.st_mtime_ns // 1_000_000, "derived-test-identity"),),
        complete=True,
    )[0]
    tag = source.create_tag("保留标签")
    source.set_document_tags(document.id, (tag.id,))
    source.set_favorite(document.id, True)
    source.set_setting("editor_path", "never-activate.exe")
    source.set_setting("quick_open_hotkey_enabled", "1")
    source.set_setting("__maintenance.hash_verified.test", "999999999999")
    source.close()
    smart_rows = []
    if version == 2:
        reference_library = str(uuid.uuid4()) if dangling == "library" else library.id
        reference_tag = str(uuid.uuid4()) if dangling == "tag" else tag.id
        name = "中文组合筛选"
        smart_rows = [{
            "id": str(uuid.uuid4()), "name": name, "name_key": normalize_text(name),
            "query_json": encode_query({
                "text": '"中文" %_', "mode": "all", "library_id": reference_library,
                "folder": "nested", "tag_ids": [reference_tag], "favorites_only": True,
                "recent_only": False, "include_unavailable": True,
            }),
        }]
        with closing(sqlite3.connect(source.metadata_path)) as connection:
            connection.execute(SMART_LIST_SCHEMA)
            connection.executemany(
                "INSERT INTO smart_lists(id,name,name_key,query_json) VALUES (?,?,?,?)",
                [(row["id"], row["name"], row["name_key"], row["query_json"]) for row in smart_rows],
            )
            connection.execute("PRAGMA user_version=2")
            connection.commit()
    source = Store(source.data_dir)
    json_backup = tmp_path / "candidate.json"
    source.export_backup(json_backup)
    sqlite_backup = source.backup_metadata()
    source.close()
    # JSON restore must ignore machine settings too, even if supplied manually.
    payload = json.loads(json_backup.read_text(encoding="utf-8"))
    payload["settings"] = [
        {"key": "editor_path", "value": "never-activate.exe"},
        {"key": "quick_open_hotkey_enabled", "value": "1"},
    ]
    json_backup.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    target = tmp_path / "target-data"
    target.mkdir()
    old_target = b"corrupt old metadata; preserve as recovery evidence"
    (target / "metadata.db").write_bytes(old_target)
    return Sample(
        tmp_path, source.data_dir, target, document_path, digest(document_path),
        json_backup, sqlite_backup, smart_rows, library.id, tag.id, old_target,
    )


@pytest.fixture
def sample(tmp_path):
    return make_sample(tmp_path)


def restore(sample, kind):
    recovery = RecoveryManager(sample.target_data)
    current = recovery.inspect_current()
    candidate = recovery.inspect_backup(sample.candidate(kind))
    assert candidate["smart_lists"] == len(sample.smart_rows)
    result = recovery.recover(
        sample.candidate(kind), True, current["signature"], candidate["signature"],
    )
    assert result["recovered"]
    assert digest(sample.document) == sample.document_hash
    old_copies = tuple((sample.target_data / "recovery").glob("*/old-metadata.db"))
    assert any(path.read_bytes() == sample.old_target for path in old_copies)
    return recovery


def assert_clean(sample, version):
    uri = (sample.target_data / "metadata.db").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [tuple(row) for row in connection.execute("SELECT key,value FROM settings")] == [
            ("__maintenance.recovery_requires_reindex", "1"),
        ]
        assert connection.execute("SELECT count(*) FROM document_file_identities").fetchone()[0] == 0
        if version == 2:
            rows = [dict(row) for row in connection.execute(
                "SELECT id,name,name_key,query_json FROM smart_lists ORDER BY id",
            )]
            assert rows == sorted(sample.smart_rows, key=lambda row: row["id"])
        else:
            assert connection.execute("SELECT name FROM sqlite_schema WHERE name='smart_lists'").fetchone() is None
    assert digest(sample.document) == sample.document_hash


@pytest.mark.parametrize("kind", ["json", "sqlite"])
def test_version_two_recovery_preserves_lists_but_not_machine_settings_or_identity(sample, kind):
    restore(sample, kind)
    assert_clean(sample, 2)
    assert RecoveryManager(sample.target_data).inspect_current()["status"] == "healthy"


@pytest.mark.parametrize("kind", ["json", "sqlite"])
def test_legacy_backup_stays_schema_one_without_feature_table(tmp_path, kind):
    sample = make_sample(tmp_path, version=1)
    restore(sample, kind)
    assert_clean(sample, 1)


@pytest.mark.parametrize("kind", ["json", "sqlite"])
@pytest.mark.parametrize("dangling", ["tag", "library"])
def test_dangling_reference_is_preserved_and_never_widens_restored_query(tmp_path, kind, dangling):
    sample = make_sample(tmp_path, dangling=dangling)
    restore(sample, kind)
    assert_clean(sample, 2)
    store = Store(sample.target_data)
    try:
        saved = store.list_smart_lists()[0]
        original = decode_query(sample.smart_rows[0]["query_json"])
        assert query_payload(saved.query) == query_payload(original)
        assert not saved.available and saved.warning
        with pytest.raises(NotesError):
            store.search_smart_list(saved.id)
        assert store.get_library(sample.library_id).id == sample.library_id
        assert len(store.list_documents()) == 1
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["json", "sqlite"])
def test_empty_version_two_list_collection_is_not_downgraded(sample, kind):
    sample.smart_rows.clear()
    if kind == "json":
        payload = json.loads(sample.json_backup.read_text(encoding="utf-8"))
        payload["smart_lists"] = []
        sample.json_backup.write_text(json.dumps(payload), encoding="utf-8")
    else:
        with closing(sqlite3.connect(sample.sqlite_backup)) as connection:
            connection.execute("DELETE FROM smart_lists")
            connection.commit()
    restore(sample, kind)
    assert_clean(sample, 2)


@pytest.mark.parametrize("mutation", [
    "v2_missing_field", "v1_nonempty", "future_format", "missing_column", "unknown_column",
    "bad_name_key", "noncanonical_id", "duplicate_id", "duplicate_name",
    "bad_query_version", "unknown_query_field", "duplicate_query_key", "too_many",
])
def test_malformed_json_list_or_version_rejected_without_modifying_live(sample, mutation):
    payload = json.loads(sample.json_backup.read_text(encoding="utf-8"))
    row = payload["smart_lists"][0]
    if mutation == "v2_missing_field":
        del payload["smart_lists"]
    elif mutation == "v1_nonempty":
        payload["format_version"] = 1
    elif mutation == "future_format":
        payload["format_version"] = 3
    elif mutation == "missing_column":
        del row["query_json"]
    elif mutation == "unknown_column":
        row["sql"] = "SELECT * FROM documents"
    elif mutation == "bad_name_key":
        row["name_key"] = "incorrect"
    elif mutation == "noncanonical_id":
        row["id"] = row["id"].replace("-", "")
    elif mutation in ("duplicate_id", "duplicate_name"):
        other = dict(row)
        if mutation == "duplicate_id":
            other["name"] = other["name_key"] = "different"
        else:
            other["id"] = str(uuid.uuid4())
        payload["smart_lists"].append(other)
    elif mutation == "bad_query_version":
        row["query_json"] = '{"version":2,"query":{}}'
    elif mutation == "unknown_query_field":
        row["query_json"] = '{"version":1,"query":{"sql":"SELECT * FROM documents"}}'
    elif mutation == "duplicate_query_key":
        row["query_json"] = '{"version":1,"query":{"text":"narrow","text":""}}'
    elif mutation == "too_many":
        payload["smart_lists"] = [
            {"id": str(uuid.uuid4()), "name": f"list-{index}", "name_key": f"list-{index}",
             "query_json": encode_query({})} for index in range(101)
        ]
    sample.json_backup.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    before = (sample.target_data / "metadata.db").read_bytes()
    with pytest.raises(NotesError):
        RecoveryManager(sample.target_data).inspect_backup(sample.json_backup)
    assert (sample.target_data / "metadata.db").read_bytes() == before
    assert digest(sample.document) == sample.document_hash
    assert not (sample.target_data / "recovery").exists()


@pytest.mark.parametrize("mutation", [
    "v2_missing_table", "v1_nonempty", "future_version", "extra_column", "wrong_type",
    "missing_unique", "malformed_query", "trigger", "view",
])
def test_malformed_sqlite_list_schema_and_rows_are_not_imported(sample, mutation):
    with closing(sqlite3.connect(sample.sqlite_backup)) as connection:
        if mutation == "v2_missing_table":
            connection.execute("DROP TABLE smart_lists")
        elif mutation == "v1_nonempty":
            connection.execute("PRAGMA user_version=1")
        elif mutation == "future_version":
            connection.execute("PRAGMA user_version=3")
        elif mutation == "extra_column":
            connection.execute("ALTER TABLE smart_lists ADD COLUMN extra TEXT")
        elif mutation in ("wrong_type", "missing_unique"):
            connection.execute("DROP TABLE smart_lists")
            if mutation == "wrong_type":
                connection.execute("""CREATE TABLE smart_lists(
                    id INTEGER PRIMARY KEY,name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE,query_json TEXT NOT NULL)""")
            else:
                connection.execute("""CREATE TABLE smart_lists(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,
                    name_key TEXT NOT NULL,query_json TEXT NOT NULL)""")
        elif mutation == "malformed_query":
            connection.execute("UPDATE smart_lists SET query_json=?", ('{"version":99,"query":{}}',))
        elif mutation == "trigger":
            connection.execute("""CREATE TRIGGER unwanted_smart_trigger AFTER INSERT ON smart_lists
                                  BEGIN DELETE FROM tags; END""")
        elif mutation == "view":
            connection.execute("DROP TABLE smart_lists")
            connection.execute("CREATE VIEW smart_lists AS SELECT id,name,name_key,'' AS query_json FROM tags")
        connection.commit()
    before = (sample.target_data / "metadata.db").read_bytes()
    with pytest.raises(NotesError):
        RecoveryManager(sample.target_data).inspect_backup(sample.sqlite_backup)
    assert (sample.target_data / "metadata.db").read_bytes() == before
    assert digest(sample.document) == sample.document_hash


@pytest.mark.parametrize("kind", ["json", "sqlite"])
def test_legacy_version_with_empty_feature_payload_is_safe(tmp_path, kind):
    from notes.infrastructure.store import SMART_LIST_SCHEMA
    sample = make_sample(tmp_path, version=1)
    if kind == "json":
        payload = json.loads(sample.json_backup.read_text(encoding="utf-8"))
        payload["smart_lists"] = []
        sample.json_backup.write_text(json.dumps(payload), encoding="utf-8")
    else:
        with closing(sqlite3.connect(sample.sqlite_backup)) as connection:
            connection.execute(SMART_LIST_SCHEMA)
            connection.commit()
    restore(sample, kind)
    assert_clean(sample, 1)


def test_current_profile_preflight_is_strict_about_corrupt_smart_rows(sample):
    recovery = RecoveryManager(sample.source_data)
    assert recovery.inspect_current()["status"] == "healthy"
    with closing(sqlite3.connect(sample.source_data / "metadata.db")) as connection:
        connection.execute("UPDATE smart_lists SET query_json=?", ('{"version":123}',))
        connection.commit()
    before = (sample.source_data / "metadata.db").read_bytes()
    state = recovery.inspect_current()
    assert state["status"] == "corrupt" and state["recoverable"]
    assert (sample.source_data / "metadata.db").read_bytes() == before
    assert digest(sample.document) == sample.document_hash


def run_child(sample, action, *, kind="json", stage=None):
    report = sample.base / f"smart-{action}-{kind}-{stage or 'complete'}.json"
    command = [sys.executable, str(CHILD), "--data-dir", str(sample.target_data),
               "--action", action, "--report", str(report)]
    if action == "recover":
        command.extend(("--backup", str(sample.candidate(kind))))
    if stage:
        command.extend(("--crash-stage", stage))
    environment = dict(os.environ, PYTHONPATH=str(PROJECT / "src"))
    result = subprocess.run(
        command, env=environment, cwd=PROJECT, capture_output=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    payload = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
    return result.returncode, payload


@pytest.mark.parametrize("kind", ["json", "sqlite"])
@pytest.mark.parametrize("stage", ["prepared", "metadata_replaced_unjournaled"])
def test_real_child_interruption_resumes_with_lists_and_original_bytes(sample, kind, stage):
    code, report = run_child(sample, "recover", kind=kind, stage=stage)
    assert code == 73 and report["crashed_at"] == stage, report
    code, report = run_child(sample, "resume")
    assert code == 0 and report["result"]["status"] == "completed", report
    assert_clean(sample, 2)
    protected = tuple((sample.target_data / "recovery").glob("*/old-metadata.db"))
    assert any(path.read_bytes() == sample.old_target for path in protected)


def test_clean_state_reverification_rejects_resigned_bad_smart_query(sample):
    code, report = run_child(sample, "recover", stage="prepared")
    assert code == 73, report
    journals = tuple((sample.target_data / "recovery").glob("*/journal.json"))
    assert len(journals) == 1
    journal = journals[0]
    new_database = journal.parent / "new-metadata.db"
    with closing(sqlite3.connect(new_database)) as connection:
        connection.execute("UPDATE smart_lists SET query_json=?", ('{"version":999,"query":{}}',))
        connection.commit()
    wrapper = json.loads(journal.read_text(encoding="utf-8"))
    wrapper["payload"]["new"] = {"size": new_database.stat().st_size, "sha256": digest(new_database)}
    encoded = json.dumps(wrapper["payload"], ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    wrapper["sha256"] = hashlib.sha256(encoded).hexdigest()
    journal.write_text(json.dumps(wrapper, ensure_ascii=False), encoding="utf-8")
    code, report = run_child(sample, "resume")
    assert code == 0 and report["result"]["status"] == "rolled_back", report
    assert (sample.target_data / "metadata.db").read_bytes() == sample.old_target
    assert digest(sample.document) == sample.document_hash
