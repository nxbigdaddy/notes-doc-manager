"""Reject SQLite releases affected by the documented WAL-reset corruption bug."""

import sqlite3

from notes.domain.models import NotesError


def sqlite_wal_fixed(version=None) -> bool:
    """Official fixes: 3.51.3+, 3.50.7 backport, and 3.44.6 backport."""
    current = tuple(sqlite3.sqlite_version_info if version is None else version)
    return (current >= (3, 51, 3)
            or (3, 50, 7) <= current < (3, 51, 0)
            or (3, 44, 6) <= current < (3, 45, 0))


def require_safe_runtime():
    if not sqlite_wal_fixed():
        raise NotesError(
            "unsafe_sqlite_runtime",
            "当前SQLite运行库不满足已知WAL问题的修复要求。请使用本项目提供的新版完整程序包"
            "或按README配置项目私有运行环境；未打开或修改管理数据库。",
        )
