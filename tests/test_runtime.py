import pytest

from notes.domain.models import NotesError
from notes.infrastructure import runtime


@pytest.mark.parametrize("version", [(3, 50, 4), (3, 50, 6), (3, 51, 2), (3, 44, 5), (3, 45, 0)])
def test_affected_wal_versions_are_not_declared_fixed(version):
    assert runtime.sqlite_wal_fixed(version) is False


@pytest.mark.parametrize("version", [(3, 53, 4), (3, 51, 3), (3, 50, 7), (3, 44, 6)])
def test_official_fixed_branches_are_accepted(version):
    assert runtime.sqlite_wal_fixed(version) is True


def test_default_runtime_gate_uses_loaded_library_not_python_patch_label(monkeypatch):
    monkeypatch.setattr(runtime.sqlite3, "sqlite_version_info", (3, 50, 4))
    with pytest.raises(NotesError) as error:
        runtime.require_safe_runtime()
    assert error.value.code == "unsafe_sqlite_runtime"
