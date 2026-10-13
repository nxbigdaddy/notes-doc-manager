"""Native SQLite diagnostic connections must close before fixture cleanup."""

from pathlib import Path

from notes import bootstrap


def test_selftest_closes_diagnostic_connections_and_removes_owned_fixtures(tmp_path, monkeypatch):
    original = bootstrap.tempfile.TemporaryDirectory
    created = []

    def local_fixture(*args, **kwargs):
        kwargs["dir"] = tmp_path
        result = original(*args, **kwargs)
        created.append(Path(result.name))
        return result

    monkeypatch.setattr(bootstrap.tempfile, "TemporaryDirectory", local_fixture)
    result = bootstrap.self_test()
    assert all(result["self_test"].values())
    assert result["self_test"]["external_content_index"]
    assert result["self_test"]["legacy_cache_untouched"]
    assert created and all(not path.exists() for path in created)
