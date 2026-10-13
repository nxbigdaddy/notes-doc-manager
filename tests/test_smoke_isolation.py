import pytest

from notes import bootstrap


@pytest.mark.parametrize("flag", ["--gui-smoke", "--quick-smoke", "--self-test"])
def test_diagnostics_never_seed_or_scan_a_user_selected_data_directory(tmp_path, flag):
    data = tmp_path / "existing-data"
    data.mkdir()
    metadata = data / "metadata.db"
    metadata.write_bytes(b"user data must not be opened by a smoke test")
    before = metadata.read_bytes()
    with pytest.raises(SystemExit) as error:
        bootstrap.main([flag, "--data-dir", str(data)])
    assert error.value.code == 2
    assert metadata.read_bytes() == before
    assert sorted(item.name for item in data.iterdir()) == ["metadata.db"]
