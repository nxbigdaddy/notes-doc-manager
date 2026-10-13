import os
from unittest.mock import patch

import pytest

from notes.domain.models import NotesError
from notes.infrastructure.editor import (
    _external_environment,
    detect_editor,
    open_document,
    validate_editor,
)


def test_editor_requires_absolute_exe(tmp_path):
    exe = tmp_path / "Notepad3.exe"
    exe.touch()
    assert validate_editor(str(exe)) == str(exe)
    for path in ("Notepad3.exe", str(tmp_path / "run.cmd"), str(tmp_path)):
        with pytest.raises(NotesError):
            validate_editor(path)


def test_editor_launch_is_list_and_shell_free(tmp_path):
    exe = tmp_path / "Notepad3.exe"
    exe.touch()
    document = tmp_path / "中文 & (😀).txt"
    document.touch()
    with patch("notes.infrastructure.editor.subprocess.Popen") as popen:
        open_document(str(exe), document, line=9)
    assert popen.call_args.args[0] == [str(exe), "/g", "9", str(document)]
    assert popen.call_args.kwargs["shell"] is False
    assert popen.call_args.kwargs["close_fds"] is True


@pytest.mark.parametrize("line", [0, -1, "/bad", True, 2**40])
def test_editor_rejects_invalid_line(tmp_path, line):
    exe = tmp_path / "Notepad3.exe"
    exe.touch()
    doc = tmp_path / "test.txt"
    doc.touch()
    with patch("notes.infrastructure.editor.subprocess.Popen") as popen:
        with pytest.raises(NotesError):
            open_document(str(exe), doc, line=line)
        popen.assert_not_called()


def test_detection_never_launches():
    with patch("notes.infrastructure.editor.subprocess.Popen") as popen:
        assert isinstance(detect_editor(), str)
        popen.assert_not_called()


def test_frozen_path_sanitized(tmp_path):
    bundle = tmp_path / "bundle"
    outside = tmp_path / "outside"
    path = os.pathsep.join((str(bundle), str(bundle / "dlls"), str(outside)))
    with patch("sys.frozen", True, create=True), patch("sys._MEIPASS", str(bundle), create=True):
        with patch.dict(os.environ, {"PATH": path}):
            assert _external_environment()["PATH"] == str(outside)
