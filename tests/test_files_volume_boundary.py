"""Volume eligibility must be checked before any path/contents are inspected."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from notes.domain.models import Library, NotesError
from notes.infrastructure import files, monitor as monitoring


@pytest.mark.parametrize("operation", [
    "register", "validate", "entry", "scan", "folders", "preview", "index", "create",
])
def test_rejected_volume_never_reaches_path_or_content_io(tmp_path, operation):
    root = tmp_path / "documents"
    root.mkdir()
    source = root / "document.txt"
    source.write_text("original", encoding="utf-8")
    rejected = NotesError("unsupported_volume", "Only local fixed NTFS is supported")
    with patch.object(files, "_local_volume", side_effect=rejected) as volume:
        with patch.object(files, "_check_chain", side_effect=AssertionError("Path probing prohibited")):
            with patch.object(Path, "stat", side_effect=AssertionError("stat prohibited")):
                with patch.object(files, "_shared_reader", side_effect=AssertionError("Body IO prohibited")):
                    if operation == "scan":
                        result = files.scan_directory(root)
                        assert not result.complete and result.availability == "offline"
                        assert result.entries == ()
                    elif operation in ("preview", "index"):
                        result = files.read_document(source, preview=operation == "preview")
                        assert result.status == "error"
                        assert result.error_code == "unsupported_volume"
                        assert result.text == ""
                    else:
                        with pytest.raises(NotesError) as error:
                            if operation == "register":
                                files.validate_library_root(str(root), tmp_path / "data")
                            elif operation == "validate":
                                files.validate_document_path(root, "document.txt")
                            elif operation == "entry":
                                files.get_file_entry(root, "document.txt")
                            elif operation == "folders":
                                files.list_folders(root)
                            elif operation == "create":
                                files.create_document(root, "", "new.txt")
                        assert error.value.code == "unsupported_volume"
    volume.assert_called_once()
    assert not (root / "new.txt").exists()
    assert source.read_text(encoding="utf-8") == "original"


@pytest.mark.skipif(os.name != "nt", reason="Windows volume APIs")
@pytest.mark.parametrize("drive_type", [0, 1, 2, 4, 5, 6])
def test_nonfixed_drive_rejected_before_volume_filesystem_query(tmp_path, drive_type):
    kernel = SimpleNamespace(GetDriveTypeW=Mock(return_value=drive_type),
                             GetVolumeInformationW=Mock())
    with patch.object(files.ctypes, "WinDLL", return_value=kernel):
        with pytest.raises(NotesError) as error:
            files._local_volume(tmp_path)
    assert error.value.code == "unsupported_volume"
    kernel.GetVolumeInformationW.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows volume APIs")
def test_fixed_non_ntfs_volume_is_rejected(tmp_path):
    def volume_info(_root, _name, _size, _serial, _max_component, _flags, filesystem, _length):
        filesystem.value = "exFAT"
        return 1
    kernel = SimpleNamespace(GetDriveTypeW=Mock(return_value=3),
                             GetVolumeInformationW=Mock(side_effect=volume_info))
    with patch.object(files.ctypes, "WinDLL", return_value=kernel):
        with pytest.raises(NotesError) as error:
            files._local_volume(tmp_path)
    assert error.value.code == "unsupported_volume"


def test_monitor_rejects_recovered_unsupported_root_before_subscription(tmp_path):
    monitor = monitoring.FileMonitor(lambda identifier: None)
    try:
        with patch.object(monitoring, "_local_volume",
                          side_effect=NotesError("unsupported_volume", "Rejected")):
            with patch.object(monitoring, "_check_chain",
                              side_effect=AssertionError("Do not inspect root")):
                monitor.watch([Library("one", "One", str(tmp_path))])
                health = monitor.health()
        assert health["degraded"]
        batch = monitor.take_change_batch("one")
        assert not batch.reliable and batch.moves == () and batch.paths is None
    finally:
        monitor.close()


def test_monitor_detects_volume_remapping_before_root_chain_probe(tmp_path):
    monitor = monitoring.FileMonitor(lambda identifier: None)
    try:
        monitor.watch([Library("one", "One", str(tmp_path))])
        assert not monitor.health()["degraded"]
        with patch.object(monitoring, "_local_volume",
                          side_effect=NotesError("unsupported_volume", "Remapped")):
            with patch.object(monitoring, "_check_chain",
                              side_effect=AssertionError("Do not inspect remapped root")):
                assert monitor.health()["degraded"]
        assert not monitor.take_change_batch("one").reliable
    finally:
        monitor.close()


def test_registering_library_does_not_probe_previously_registered_roots(tmp_path):
    root = tmp_path / "new-library"
    root.mkdir()
    old_root = tmp_path / "saved-offline-library"
    original_resolve = Path.resolve
    def guarded_resolve(path, *args, **kwargs):
        assert path != old_root, "Existing library root must not be resolved"
        return original_resolve(path, *args, **kwargs)
    with patch.object(Path, "resolve", guarded_resolve):
        assert files.validate_library_root(str(root), tmp_path / "data", (str(old_root),)) == root


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-letter boundary")
def test_saved_z_drive_is_only_lexically_compared_and_never_probed(tmp_path):
    root = tmp_path / "local-library"
    root.mkdir()
    saved = Path(r"Z:\previously-local-library")
    original_resolve = Path.resolve
    original_stat = Path.stat
    def guarded_resolve(path, *args, **kwargs):
        assert path.drive.upper() != "Z:", "Never resolve a saved remapped drive"
        return original_resolve(path, *args, **kwargs)
    def guarded_stat(path, *args, **kwargs):
        assert path.drive.upper() != "Z:", "Never stat a saved remapped drive"
        return original_stat(path, *args, **kwargs)
    with patch.object(Path, "resolve", guarded_resolve), patch.object(Path, "stat", guarded_stat):
        result = files.validate_library_root(str(root), tmp_path / "data", (str(saved),))
    assert result == root
