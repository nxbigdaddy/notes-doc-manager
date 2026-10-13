"""Independent NTFS/service acceptance tests for conservative observed moves.

All renamed, replaced, copied or deleted paths are generated inside pytest's
temporary directory. Hints are evidence, not authorization to merge metadata.
"""
from dataclasses import replace
import os
from pathlib import Path
import shutil
import time

import pytest

from notes.application.service import ApplicationService
from notes.domain.models import MoveHint
from notes.infrastructure import files

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Real Windows NTFS move acceptance")


def owned_path(path, boundary):
    """Validate the absolute source/destination before fixture file operations."""
    path = Path(path)
    assert path.resolve().is_relative_to(Path(boundary).resolve())
    assert path.resolve() != Path(boundary).resolve()
    return path


def move_file(source, target, boundary, *, replace_target=False):
    source = owned_path(source, boundary)
    target = owned_path(target, boundary)
    if replace_target:
        os.replace(source, target)
    else:
        source.rename(target)


def hint(source, target, *, age=0, directory=False, synthetic=False):
    return MoveHint(source, target, time.monotonic() - age,
                    is_directory=directory, is_synthetic=synthetic)


def scan_hints(service, library, *moves, reliable=True):
    return service.call("scan", {"library_id": library.id, "moves": tuple(moves),
                                 "moves_reliable": reliable})


def annotate(service, document, name="保留原标签"):
    tag = service.call("create_tag", {"name": name})
    service.call("set_document_tags", {"document_id": document.id, "tag_ids": [tag.id]})
    service.call("set_favorite", {"document_id": document.id, "value": True})
    service.call("set_encoding", {"document_id": document.id, "encoding": "utf-8"})
    return tag


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "原文件.txt").write_text("原始中文内容与唯一标识XYZ。", encoding="utf-8")
    service = ApplicationService(tmp_path / "metadata", monitoring=False)
    try:
        library = service.call("add_library", {"root_path": str(root), "name": "独立QA库"})
        service.call("scan", {"library_id": library.id})
        original = service.call("query").items[0].document
        tag = annotate(service, original)
        yield service, library, root, original, tag
    finally:
        service.close()


def assert_annotations(document, tag):
    assert document.is_favorite
    assert document.encoding_override == "utf-8"
    assert tuple(item.id for item in document.tags) == (tag.id,)


@pytest.mark.parametrize("relative", ["改名后.txt", "新文件夹/跨目录.txt"])
def test_observed_same_library_move_preserves_identity_and_annotations(corpus, relative):
    service, library, root, original, tag = corpus
    target = root / relative
    target.parent.mkdir(exist_ok=True)
    original_bytes = (root / original.relative_path).read_bytes()
    move_file(root / original.relative_path, target, root)
    scan_hints(service, library, hint(original.relative_path, relative))
    relocated = service.store.get_document(original.id)
    assert relocated.relative_path == relative
    assert relocated.availability == "present"
    assert_annotations(relocated, tag)
    assert service.call("query").total == 1
    assert target.read_bytes() == original_bytes
    preview = service.call("preview", {"document_id": original.id})
    assert preview.path == str(target)
    assert "唯一标识XYZ" in preview.read.text


@pytest.mark.parametrize("annotation", ["tag", "favorite", "encoding", "recent"])
def test_target_with_manual_information_is_never_merged(corpus, annotation):
    service, library, root, original, tag = corpus
    target = root / "已管理目标.txt"
    target.write_text("已有目标", encoding="utf-8")
    service.call("scan", {"library_id": library.id})
    target_doc = next(item.document for item in service.call("query").items
                      if item.document.relative_path == target.name)
    if annotation == "tag":
        target_tag = service.call("create_tag", {"name": "目标自己的标签"})
        service.call("set_document_tags", {"document_id": target_doc.id,
                                           "tag_ids": [target_tag.id]})
    elif annotation == "favorite":
        service.call("set_favorite", {"document_id": target_doc.id, "value": True})
    elif annotation == "encoding":
        service.call("set_encoding", {"document_id": target_doc.id, "encoding": "utf-8"})
    else:
        # Record an editor-launch request without launching any external program.
        service.store.mark_opened(target_doc.id)
    target_before = service.store.get_document(target_doc.id)
    move_file(root / original.relative_path, target, root, replace_target=True)
    scan_hints(service, library, hint(original.relative_path, target.name))
    retained = service.store.get_document(original.id)
    assert retained.relative_path == original.relative_path
    assert retained.availability == "missing"
    assert_annotations(retained, tag)
    target_after = service.store.get_document(target_doc.id)
    assert target_after.relative_path == target.name
    assert target_after.tags == target_before.tags
    assert target_after.is_favorite == target_before.is_favorite
    assert target_after.encoding_override == target_before.encoding_override
    assert target_after.last_open_requested_at == target_before.last_open_requested_at


def test_copy_then_delete_does_not_claim_identity_even_with_matching_text_and_hint(corpus):
    service, library, root, original, tag = corpus
    source = owned_path(root / original.relative_path, root)
    target = owned_path(root / "复制件.txt", root)
    source_identity = source.stat().st_ino
    shutil.copy2(source, target)
    assert target.stat().st_ino != source_identity
    source.unlink()
    scan_hints(service, library, hint(source.name, target.name))
    retained = service.store.get_document(original.id)
    assert retained.relative_path == original.relative_path
    assert retained.availability == "missing"
    assert_annotations(retained, tag)
    visible = service.call("query").items
    assert len(visible) == 1 and visible[0].document.id != original.id
    assert not visible[0].document.tags


@pytest.mark.parametrize("case", ["unreliable", "synthetic", "stale", "future", "directory"])
def test_unsafe_hints_degrade_to_retained_missing_record(corpus, case):
    service, library, root, original, tag = corpus
    target = root / "新路径.txt"
    move_file(root / original.relative_path, target, root)
    age = 301 if case == "stale" else -60 if case == "future" else 0
    evidence = hint(original.relative_path, target.name, age=age,
                    synthetic=case == "synthetic", directory=case == "directory")
    scan_hints(service, library, evidence, reliable=case != "unreliable")
    retained = service.store.get_document(original.id)
    assert retained.relative_path == original.relative_path
    assert retained.availability == "missing"
    assert_annotations(retained, tag)
    visible = service.call("query").items
    assert len(visible) == 1 and visible[0].document.id != original.id


@pytest.mark.parametrize("case", ["chain", "source_conflict", "target_conflict"])
def test_chained_or_ambiguous_move_pairs_do_not_guess_identity(corpus, case):
    service, library, root, original, tag = corpus
    middle = root / "中间路径.txt"
    final = root / "最终路径.txt"
    if case == "chain":
        move_file(root / original.relative_path, middle, root)
        move_file(middle, final, root)
        evidence = (hint(original.relative_path, middle.name), hint(middle.name, final.name))
    elif case == "source_conflict":
        move_file(root / original.relative_path, final, root)
        evidence = (hint(original.relative_path, middle.name),
                    hint(original.relative_path, final.name))
    else:
        move_file(root / original.relative_path, final, root)
        evidence = (hint(original.relative_path, final.name),
                    hint("另一个来源.txt", final.name))
    scan_hints(service, library, *evidence)
    retained = service.store.get_document(original.id)
    assert retained.relative_path == original.relative_path
    assert retained.availability == "missing"
    assert_annotations(retained, tag)
    assert service.call("query").items[0].document.id != original.id


def test_identical_duplicate_hints_do_not_prevent_unambiguous_move(corpus):
    service, library, root, original, tag = corpus
    target = root / "重复通知.txt"
    move_file(root / original.relative_path, target, root)
    evidence = hint(original.relative_path, target.name)
    scan_hints(service, library, evidence, evidence)
    retained = service.store.get_document(original.id)
    assert retained.relative_path == target.name
    assert_annotations(retained, tag)


def test_partial_unrelated_enumeration_does_not_erase_records_or_undo_proven_move(corpus, monkeypatch):
    service, library, root, original, tag = corpus
    unrelated = root / "扫描暂时不可见.txt"
    unrelated.write_text("权限异常子树模拟", encoding="utf-8")
    service.call("scan", {"library_id": library.id})
    unrelated_doc = next(hit.document for hit in service.call("query").items
                         if hit.document.relative_path == unrelated.name)
    target = root / "未知完整性.txt"
    move_file(root / original.relative_path, target, root)
    actual_scan = files.scan_directory

    def partial(*args, **kwargs):
        result = actual_scan(*args, **kwargs)
        entries = tuple(entry for entry in result.entries if entry.relative_path != unrelated.name)
        return replace(result, entries=entries, complete=False, errors=("test_incomplete",))
    with monkeypatch.context() as patch:
        patch.setattr(files, "scan_directory", partial)
        scan_hints(service, library, hint(original.relative_path, target.name))
    # Live path + identity proved the individual move independently. An unrelated
    # enumeration error must not undo that association or declare unseen files lost.
    retained = service.store.get_document(original.id)
    assert retained.relative_path == target.name and retained.availability == "present"
    assert_annotations(retained, tag)
    assert service.store.get_document(unrelated_doc.id).availability == "present"


@pytest.mark.parametrize("case", ["unknown", "overflow"])
def test_monitor_incomplete_or_overflow_batch_loses_move_authority(corpus, case):
    from watchdog.events import FileModifiedEvent, FileMovedEvent
    from notes.infrastructure.monitor import FileMonitor
    service, library, root, original, tag = corpus
    target = root / "事件不完整.txt"
    move_file(root / original.relative_path, target, root)
    monitor = FileMonitor(lambda library_id: None)
    try:
        monitor.watch([library])
        monitor._event(library.id, FileMovedEvent(str(root / original.relative_path), str(target)))
        if case == "unknown":
            monitor._dirty(library.id)
        else:
            for number in range(1001):
                monitor._event(library.id, FileModifiedEvent(str(root / f"overflow-{number}.txt")))
        batch = monitor.take_change_batch(library.id)
        assert batch.paths is None and not batch.reliable and not batch.moves
    finally:
        monitor.close()
    service.call("scan", {"library_id": library.id, "moves": batch.moves,
                          "moves_reliable": batch.reliable})
    retained = service.store.get_document(original.id)
    assert retained.relative_path == original.relative_path and retained.availability == "missing"
    assert_annotations(retained, tag)
    assert service.call("query").items[0].document.id != original.id


def test_cross_library_move_does_not_transfer_manual_information(corpus, tmp_path):
    service, library, root, original, tag = corpus
    other = tmp_path / "other-library"
    other.mkdir()
    other_library = service.call("add_library", {"root_path": str(other)})
    target = other / "跨库目标.txt"
    move_file(root / original.relative_path, target, tmp_path)
    # A malicious cross-root relative hint must not bypass the library boundary.
    scan_hints(service, library, hint(original.relative_path, "../other-library/跨库目标.txt"))
    service.call("scan", {"library_id": other_library.id})
    retained = service.store.get_document(original.id)
    assert retained.library_id == library.id and retained.availability == "missing"
    assert_annotations(retained, tag)
    moved = service.call("query", {"library_id": other_library.id}).items[0].document
    assert moved.id != original.id
    assert not moved.tags and not moved.is_favorite and moved.encoding_override is None


def test_directory_move_is_not_expanded_into_speculative_file_identity_moves(corpus):
    service, library, root, _, _ = corpus
    old_directory = root / "旧目录"
    old_directory.mkdir()
    (old_directory / "内部.txt").write_text("内部文档", encoding="utf-8")
    service.call("scan", {"library_id": library.id})
    document = next(item.document for item in service.call("query").items
                    if item.document.relative_path == "旧目录/内部.txt")
    tag = annotate(service, document, "目录内原标签")
    new_directory = root / "新目录"
    move_file(old_directory, new_directory, root)
    scan_hints(service, library, hint("旧目录", "新目录", directory=True),
               hint("旧目录/内部.txt", "新目录/内部.txt", synthetic=True))
    retained = service.store.get_document(document.id)
    assert retained.relative_path == "旧目录/内部.txt" and retained.availability == "missing"
    assert_annotations(retained, tag)


def test_atomic_replace_keeps_annotations_on_original_name_not_backup_or_temp(corpus):
    service, library, root, original, tag = corpus
    source = root / original.relative_path
    backup = root / "编辑器备份.txt"
    temporary = root / "编辑器临时.txt"
    temporary.write_text("原子保存后的全新内容", encoding="utf-8")
    move_file(source, backup, root)
    move_file(temporary, source, root)
    scan_hints(service, library, hint(original.relative_path, backup.name),
               hint(temporary.name, original.relative_path))
    retained = service.store.get_document(original.id)
    assert retained.relative_path == original.relative_path
    assert retained.availability == "present"
    assert_annotations(retained, tag)
    preview = service.call("preview", {"document_id": original.id})
    assert preview.read.text == "原子保存后的全新内容"
    backup_doc = next(item.document for item in service.call("query").items
                      if item.document.relative_path == backup.name)
    assert backup_doc.id != original.id
    assert not backup_doc.tags and not backup_doc.is_favorite
    assert backup_doc.encoding_override is None


def test_manual_relocation_refreshes_identity_for_future_observed_move(corpus):
    service, library, root, original, tag = corpus
    source = root / original.relative_path
    replacement = root / "手动关联.txt"
    # Deliberately use a different physical identity, forcing manual association.
    replacement.write_text("手动选择的新内容", encoding="utf-8")
    owned_path(source, root).unlink()
    service.call("scan", {"library_id": library.id})
    service.call("relocate_document", {"document_id": original.id,
                                      "target_path": str(replacement),
                                      "merge_unannotated": True, "confirmed": True})
    future = root / "以后再次改名.txt"
    move_file(replacement, future, root)
    scan_hints(service, library, hint(replacement.name, future.name))
    retained = service.store.get_document(original.id)
    assert retained.relative_path == future.name
    assert_annotations(retained, tag)
    assert service.call("preview", {"document_id": original.id}).read.text == "手动选择的新内容"


def test_real_watchdog_move_preserves_identity_without_injected_hint(tmp_path):
    root = tmp_path / "watched"
    root.mkdir()
    source = root / "watch原文件.txt"
    target = root / "watch新文件.txt"
    source.write_text("真实watchdog改名事件", encoding="utf-8")
    service = ApplicationService(tmp_path / "metadata", monitoring=True)
    try:
        library = service.call("add_library", {"root_path": str(root)})
        service.call("scan", {"library_id": library.id})
        original = service.call("query").items[0].document
        tag = annotate(service, original)
        assert not service._monitor.health()["degraded"]
        # Settle startup scans; no hand-built MoveHint or explicit scan follows.
        deadline = time.monotonic() + 10
        while service.call("state").busy and time.monotonic() < deadline:
            time.sleep(.02)
        move_file(source, target, root)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = service.store.get_document(original.id)
            if current.relative_path == target.name and current.availability == "present":
                break
            time.sleep(.05)
        else:
            pytest.fail("Real watcher rename did not preserve ID within 15s; "
                        f"state={service.call('state').status}; "
                        f"records={[(d.id, d.relative_path, d.availability) for d in service.store.list_documents()]}")
        assert_annotations(current, tag)
        assert service.call("preview", {"document_id": original.id}).path == str(target)
    finally:
        service.close()
