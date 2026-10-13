import os

import pytest

from notes.bootstrap import _logging, prepare_data_dir
from notes.domain.models import NotesError
from notes.infrastructure import files


def test_unsupported_volume_is_rejected_before_path_io(tmp_path, monkeypatch):
    accessed = []

    def reject(_):
        raise NotesError("unsupported_volume", "test")

    def forbidden(*args, **kwargs):
        accessed.append(True)
        raise AssertionError("Path IO before volume check")

    monkeypatch.setattr(files, "_local_volume", reject)
    monkeypatch.setattr(type(tmp_path), "lstat", forbidden)
    with pytest.raises(NotesError, match="test"):
        prepare_data_dir(tmp_path / "data")
    assert accessed == []


def test_missing_data_ancestors_are_created_but_not_metadata(tmp_path):
    target = tmp_path / "new-parent" / "data"
    assert prepare_data_dir(target) == target
    assert target.is_dir() and not (target / "metadata.db").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction guard")
def test_data_junction_is_rejected_without_creating_logs_outside(tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    _winapi.CreateJunction(str(outside), str(link))
    try:
        with pytest.raises(NotesError):
            prepare_data_dir(link / "nested")
        assert list(outside.iterdir()) == []
    finally:
        link.rmdir()


def test_logger_rejects_hardlinked_user_file(tmp_path):
    data = prepare_data_dir(tmp_path / "data")
    logs = prepare_data_dir(data / "logs")
    outside = tmp_path / "keep.txt"
    outside.write_bytes(b"never append logs here")
    try:
        os.link(outside, logs / "notes.log")
    except OSError:
        pytest.skip("Hardlinks unavailable")
    with pytest.raises(NotesError, match="日志路径"):
        _logging(data)
    assert outside.read_bytes() == b"never append logs here"
