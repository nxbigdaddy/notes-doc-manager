"""Desktop entry point and isolated diagnostic entry points."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import closing
from logging.handlers import RotatingFileHandler
from pathlib import Path

from notes import __version__
from notes.domain.models import NotesError
from notes.infrastructure.runtime import require_safe_runtime, sqlite_wal_fixed


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "Notes" if base else Path.home() / ".local" / "share" / "Notes"


def prepare_data_dir(value: Path) -> Path:
    """Validate lexical ancestors before creating an application-only directory."""
    from notes.domain.models import NotesError
    from notes.infrastructure.files import _absolute, _local_volume

    directory = _absolute(Path(value).expanduser())
    _local_volume(directory)
    for part in (*reversed(directory.parents), directory):
        try:
            info = part.lstat()
        except FileNotFoundError:
            # The previous iteration has checked the parent; do not recursively
            # mkdir through an unchecked junction or mapped network root.
            part.mkdir()
            info = part.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
                or not stat.S_ISDIR(info.st_mode)):
            raise NotesError("unsafe_data_dir", "应用数据目录必须是本地普通目录，不能经过链接")
    return directory


def _logging(data_dir: Path):
    from notes.domain.models import NotesError

    log_dir = prepare_data_dir(data_dir / "logs")
    log_path = log_dir / "notes.log"
    try:
        info = log_path.lstat()
    except FileNotFoundError:
        pass
    else:
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise NotesError("unsafe_log", "日志路径不是独立普通文件")
    handler = RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # Exclude third-party per-file diagnostic messages from the application log.
    logging.getLogger("watchdog").setLevel(logging.CRITICAL)


def environment_report() -> dict:
    import wx
    import wx.stc
    import watchdog.observers

    from importlib.metadata import PackageNotFoundError, version

    versions = {}
    for name in ("wxPython", "watchdog", "pyinstaller"):
        try:
            versions[name] = version(name)
        except PackageNotFoundError:
            versions[name] = "bundled"
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(body, tokenize='trigram')")
        connection.execute("INSERT INTO probe VALUES (?)", ("中文搜索示例",))
        fts = connection.execute(
            "SELECT count(*) FROM probe WHERE probe MATCH ?", ('"中文搜"',)
        ).fetchone()[0] == 1
    report = {
        "application": __version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
        "sqlite": sqlite3.sqlite_version,
        "sqlite_wal_reset_fixed": sqlite_wal_fixed(),
        "fts5_trigram": fts,
        "wx": wx.version(),
        "wx_stc": hasattr(wx.stc, "StyledTextCtrl"),
        "watchdog_backend": watchdog.observers.Observer.__name__,
        "packages": versions,
    }
    if getattr(sys, "frozen", False):
        provenance = Path(sys._MEIPASS) / "build-provenance.json"
        if provenance.is_file():
            report["build_provenance"] = json.loads(provenance.read_text(encoding="utf-8"))
    return report


def _write_report(report: dict, path: str | None):
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    elif sys.stdout:
        print(text)


def self_test() -> dict:
    """Only creates files beneath a fresh TemporaryDirectory; never opens an editor."""
    from notes.application.service import ApplicationService
    from notes.domain.models import MoveHint

    report = environment_report()
    with tempfile.TemporaryDirectory(prefix="notes-selftest-") as directory:
        base = Path(directory)
        documents = base / "documents"
        documents.mkdir()
        text_path = documents / "中文 & spaces.txt"
        original = "第一行\n中文搜索测试\n%_literal* \"quotes\" 🚀\n"
        text_path.write_text(original, encoding="utf-8", newline="\n")
        data_dir = base / "data"
        data_dir.mkdir()
        legacy = data_dir / "search.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute("CREATE TABLE previous_cache(marker TEXT)")
            connection.execute("INSERT INTO previous_cache VALUES ('legacy cache must not change')")
            connection.commit()
        legacy_hash = hashlib.sha256(legacy.read_bytes()).hexdigest()
        with_service = ApplicationService(data_dir, monitoring=False)
        try:
            assert with_service.store.search_path.name == "search-v2.db"
            library = with_service.call("add_library", {"root_path": str(documents)})
            with_service.call("scan", {"library_id": library.id})
            result = with_service.call("query", {"text": "中文", "mode": "content"})
            assert result.total == 1, "Chinese short search failed"
            document = result.items[0].document
            preview = with_service.call("preview", {"document_id": document.id})
            assert preview.read.text == original
            # Exercise only an isolated cache lock, with a short test timeout.
            # The production timeout and journal configuration are unchanged.
            original_connection = with_service.store._new_connection

            def short_cache_wait(path=None):
                connection = original_connection(path)
                if path == with_service.store.search_path:
                    connection.execute("PRAGMA busy_timeout=40")
                return connection

            with_service.store._new_connection = short_cache_wait
            try:
                with closing(sqlite3.connect(
                    with_service.store.search_path, isolation_level=None
                )) as blocker:
                    try:
                        blocker.execute("BEGIN IMMEDIATE")
                        try:
                            with_service.call("scan", {"library_id": library.id, "force": True})
                            raise AssertionError("Locked cache did not report a transient failure")
                        except NotesError as error:
                            assert error.code == "database_busy"
                        assert with_service.store.fts_available
                        assert not with_service.call("state").busy
                    finally:
                        blocker.rollback()
            finally:
                with_service.store._new_connection = original_connection
            with_service.call("scan", {"library_id": library.id})
            assert with_service.call("query", {"text": "中文", "mode": "content"}).total == 1
            tag = with_service.call("create_tag", {"name": "自测"})
            with_service.call(
                "set_document_tags", {"document_id": document.id, "tag_ids": [tag.id]}
            )
            with_service.call("set_favorite", {"document_id": document.id, "value": True})
            backup = base / "backup.json"
            with_service.call("export_backup", {"path": str(backup)})
            checked = with_service.call("inspect_backup", {"path": str(backup)})
            changed_tag = with_service.call("create_tag", {"name": "确认后新增的隔离样例"})
            try:
                with_service.call("restore_backup", {
                    "path": str(backup), "confirmed": True,
                    "expected_backup": checked["signature"],
                    "expected_current": checked["current_signature"],
                })
                raise AssertionError("Online restore accepted stale current state")
            except NotesError as error:
                assert error.code == "metadata_changed"
            assert changed_tag in with_service.store.list_tags()
            with_service.call("delete_tag", {"tag_id": changed_tag.id})
            try:
                with_service.call("restore_backup", {
                    "path": str(backup), "confirmed": True,
                    "expected_backup": "0" * 64, "expected_current": checked["current_signature"],
                })
                raise AssertionError("Online restore accepted an unconfirmed candidate")
            except NotesError as error:
                assert error.code == "backup_changed"
            with_service.call("restore_backup", {
                "path": str(backup), "confirmed": True,
                "expected_backup": checked["signature"], "expected_current": checked["current_signature"],
            })
            with_service.call("scan", {})
            restored = with_service.call("query", {"favorites_only": True})
            assert restored.total == 1 and restored.items[0].document.tags[0].name == "自测"
            assert text_path.read_text(encoding="utf-8") == original
            text_path.write_text("replacement-only-token\n", encoding="utf-8", newline="\n")
            with_service.call("scan", {})
            assert with_service.call("query", {"text": "中文搜索测试", "mode": "content"}).total == 0
            assert with_service.call(
                "query", {"text": "replacement-only-token", "mode": "content"}
            ).total == 1
            text_path.write_text(original, encoding="utf-8", newline="\n")
            with_service.call("scan", {})
            with closing(sqlite3.connect(with_service.store.search_path)) as index:
                assert index.execute("PRAGMA user_version").fetchone()[0] == 2
                definition = index.execute(
                    "SELECT sql FROM sqlite_master WHERE name='document_fts'"
                ).fetchone()[0]
                assert "content=" in definition.replace(" ", "").lower()
                assert "indexed_documents" in definition
                assert index.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name='document_fts_content'"
                ).fetchone()[0] == 0
            assert hashlib.sha256(legacy.read_bytes()).hexdigest() == legacy_hash
            automatic = with_service.call("automatic_backup", {"force": True})
            assert automatic["status"] == "created" and Path(automatic["path"]).is_file()
            assert with_service.call("backup_status")["last_success"] is not None
            # Simulate an external move using only this self-test's temporary fixture.
            moved = documents / "重新定位.txt"
            text_path.rename(moved)
            with_service.call("scan", {})
            selection = {"document_id": document.id, "target_path": str(moved)}
            inspection = with_service.call("inspect_relocation", selection)
            assert inspection["requires_merge_confirmation"]
            linked = with_service.call(
                "relocate_document",
                dict(selection, confirmed=True, merge_unannotated=True),
            )
            assert linked.id == document.id and linked.is_favorite
            assert linked.tags[0].name == "自测" and moved.read_text(encoding="utf-8") == original
            automatic_target = documents / "自动关联.txt"
            moved.rename(automatic_target)
            move_result = with_service.call(
                "scan",
                {"library_id": library.id, "moves_reliable": True,
                 "moves": (MoveHint(linked.relative_path, automatic_target.name, time.monotonic()),)},
            )
            assert move_result["tracked_moves"] == 1
            tracked = with_service.store.get_document(document.id)
            assert tracked.relative_path == automatic_target.name and tracked.is_favorite
            assert tracked.tags[0].name == "自测"
            assert automatic_target.read_text(encoding="utf-8") == original
            template_path = documents / "模板会议.md"
            template_preview = with_service.call(
                "preview_template", {"template_id": "meeting", "filename": template_path.name}
            )
            assert "模板会议" in template_preview.text and not template_path.exists()
            templated = with_service.call(
                "create_document",
                {"library_id": library.id, "relative_dir": "",
                 "filename": template_path.name, "template_id": "meeting"},
            )
            template_bytes = template_path.read_bytes()
            assert not template_bytes.startswith(b"\xef\xbb\xbf") and b"\r" not in template_bytes
            assert "参与人员" in template_bytes.decode("utf-8")
            assert templated.index_status == "ready"
            assert with_service.call(
                "query", {"text": "模板会议", "mode": "content"}
            ).total == 1
            from notes.infrastructure.files import read_document
            probes = base / "binary-probes"
            probes.mkdir()
            for name, text, status in (
                ("exact.txt", "a" * 98 + "\x01" * 2, "ready"),
                ("over.txt", "a" * 97 + "\x01" * 3, "skipped_binary"),
                ("contains-nul.txt", "a" * 1000 + "\x00", "skipped_binary"),
                ("c1.txt", "\x7f\x85\x9f" * 100, "ready"),
            ):
                path = probes / name
                content = text.encode("utf-8")
                path.write_bytes(content)
                classified = read_document(path)
                assert classified.status == status, (
                    name, classified.status, classified.error_code, classified.message
                )
                assert path.read_bytes() == content
                if status == "ready":
                    assert classified.text == text
            smart = with_service.call(
                "create_smart_list",
                {"name": "自测收藏组合", "query": {
                    "library_id": library.id, "tag_ids": [tracked.tags[0].id],
                    "favorites_only": True,
                }},
            )
            smart_result = with_service.call("query", {"smart_list_id": smart.id})
            assert smart_result.total == 1 and smart_result.items[0].document.id == document.id
            recovery_backup = base / "startup-recovery.json"
            with_service.call("export_backup", {"path": str(recovery_backup)})
            assert json.loads(recovery_backup.read_text(encoding="utf-8"))["format_version"] == 2
            report["self_test"] = {
                "scan": True, "unicode_search": True, "preview": True,
                "tags": True, "favorites": True, "backup_restore": True,
                "source_unchanged": True,
                "automatic_backup": True,
                "missing_file_relocation": True,
                "external_content_index": True,
                "index_replacement": True,
                "legacy_cache_untouched": True,
                "observed_file_move": True,
                "built_in_templates": True,
                "smart_lists": True,
                "online_restore_binding": True,
                "binary_classification": True,
                "transient_index_lock": True,
            }
        finally:
            with_service.close()
        # Exercise startup recovery only against this deliberately corrupted
        # synthetic metadata database. No user database is ever selected here.
        from notes.application.startup import open_application
        from notes.infrastructure.recovery import RecoveryManager

        broken = data_dir / "metadata.db"
        broken.write_bytes(b"notes isolated self-test: deliberately damaged metadata")
        manager = RecoveryManager(data_dir)
        current = manager.inspect_current()
        candidate = manager.inspect_backup(recovery_backup)
        assert current["status"] == "corrupt" and current["recoverable"]
        recovered = manager.recover(
            recovery_backup, confirmed=True, expected_current=current["signature"],
            expected_backup=candidate["signature"],
        )
        assert recovered["recovered"] and manager.inspect_current()["status"] == "healthy"
        restarted = open_application(
            data_dir, dispatcher=None, present_recovery=lambda *_: False,
            service_factory=lambda path, dispatcher: ApplicationService(
                path, dispatcher=dispatcher, monitoring=False
            ),
        )
        try:
            restarted.call("scan")
            result = restarted.call("query", {"favorites_only": True})
            assert result.total == 1 and result.items[0].document.id == document.id
            assert result.items[0].document.tags[0].name == "自测"
            assert restarted.call("state").editor_path == ""
            assert restarted.call("state").smart_lists[0].id == smart.id
            saved_result = restarted.call("query", {"smart_list_id": smart.id})
            assert saved_result.total == 1 and saved_result.items[0].document.id == document.id
            assert automatic_target.read_text(encoding="utf-8") == original
            report["self_test"]["startup_recovery"] = True
        finally:
            restarted.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Notes · 本地文档管理器")
    parser.add_argument("--data-dir", type=Path, help="isolated application data directory")
    parser.add_argument("--self-test", action="store_true", help="isolated non-GUI checks")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--gui-smoke", action="store_true", help="hidden window smoke test")
    parser.add_argument("--quick-smoke", action="store_true", help="isolated hidden quick-open query")
    parser.add_argument("--report", help="write diagnostic result to this JSON file")
    parser.add_argument("--smoke-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    if args.quick_smoke:
        args.gui_smoke = True
    if (args.gui_smoke or args.self_test) and args.data_dir is not None:
        parser.error("Smoke/self-tests always use isolated temporary data; --data-dir is not allowed")
    try:
        if args.diagnostics:
            _write_report(environment_report(), args.report)
            return 0
        require_safe_runtime()
        if args.self_test:
            _write_report(self_test(), args.report)
            return 0
        return _desktop(args)
    except Exception as exc:
        if args.self_test or args.diagnostics or args.gui_smoke:
            _write_report({"ok": False, "error_type": type(exc).__name__}, args.report)
            if not getattr(sys, "frozen", False):
                raise
            return 1
        # Detailed source diagnostics are opt-in; normal logs never include filenames/content.
        logging.error("startup_failed error_type=%s", type(exc).__name__)
        import wx

        app = wx.GetApp() or wx.App(False)
        wx.MessageBox(
            (exc.message if isinstance(exc, NotesError) else
             "程序未能启动。请检查数据目录权限或管理信息备份；已有文档不会被修改。"),
            "Notes 启动失败", wx.OK | wx.ICON_ERROR,
        )
        del app
        return 1


def _desktop(args) -> int:
    import wx

    from notes.application.startup import open_application
    from notes.infrastructure.instance import SingleInstance
    from notes.ui.main_frame import MainFrame
    from notes.ui.recovery_dialog import RecoveryDialog

    # Smoke must not touch production app data, even if --data-dir was omitted.
    temporary = tempfile.TemporaryDirectory(prefix="notes-gui-smoke-") if args.gui_smoke else None
    data_dir = prepare_data_dir(
        args.data_dir or (Path(temporary.name) / "data" if temporary else default_data_dir())
    )
    instance = SingleInstance(data_dir)
    if not instance.primary:
        instance.close()
        if temporary:
            temporary.cleanup()
        return 0
    app = None
    service = None
    frame = None
    smoke_ok = False
    quick_smoke_ok = False
    try:
        _logging(data_dir)
        app = wx.App(False)
        app.SetAppName("Notes")

        def recover(manager, status):
            if args.gui_smoke:
                # Automated smoke never waits for an interactive choice.
                raise RuntimeError("Unexpected recovery requirement in isolated GUI smoke")
            with RecoveryDialog(None, manager, status) as dialog:
                return dialog.ShowModal() == wx.ID_OK and not dialog.exit_requested

        service = open_application(
            data_dir, dispatcher=wx.CallAfter, present_recovery=recover,
        )
        if service is None:
            return 0
        if args.quick_smoke:
            fixture = Path(temporary.name) / "documents"
            fixture.mkdir()
            (fixture / "quick-start.txt").write_text("isolated quick-open fixture", encoding="utf-8")
            # Diagnostic-only setup before the event loop, never a user library.
            library = service.call("add_library", {"root_path": str(fixture)})
            service.call("scan", {"library_id": library.id})
        frame = MainFrame(service)
        app.SetTopWindow(frame)
        frame.start_activation_poll(instance.activation_requested)
        if args.gui_smoke:
            frame.Hide()
            quick = None
            if args.quick_smoke:
                from notes.ui.quick_open import QuickOpenDialog

                quick = QuickOpenDialog(frame, service)
                quick.Hide()
                quick.search.ChangeValue("quick-start")
                quick._query()

            def finish():
                nonlocal smoke_ok, quick_smoke_ok
                smoke_ok = bool(frame and frame.GetChildren())
                if quick is not None:
                    quick_smoke_ok = bool(
                        not quick._query_pending and len(quick.hits) == 1
                        and quick.hits[0].document.file_name == "quick-start.txt"
                    )
                    quick.Destroy()
                frame.Close()

            if quick is not None:
                deadline = time.monotonic() + max(5.0, args.smoke_seconds)

                def await_quick():
                    if (quick.hits and not quick._query_pending) or time.monotonic() >= deadline:
                        finish()
                    else:
                        wx.CallLater(100, await_quick)
                wx.CallLater(100, await_quick)
            else:
                wx.CallLater(max(200, int(args.smoke_seconds * 1000)), finish)
        else:
            frame.Show()
        app.MainLoop()
        if args.gui_smoke:
            report = environment_report()
            report["gui_smoke"] = smoke_ok
            if args.quick_smoke:
                report["quick_smoke"] = quick_smoke_ok
            _write_report(report, args.report)
        return 0 if (not args.gui_smoke or
                     (smoke_ok and (not args.quick_smoke or quick_smoke_ok))) else 1
    finally:
        if service:
            service.close()
        instance.close()
        # Release logging file handles before removing an isolated smoke directory.
        logging.shutdown()
        if temporary:
            temporary.cleanup()
