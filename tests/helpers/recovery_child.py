"""Isolated crash injector. Only this child process calls os._exit."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from notes.infrastructure.recovery import RecoveryManager


def report(path: Path, payload: dict):
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--action", choices=("recover", "resume"), required=True)
    parser.add_argument("--crash-stage")
    args = parser.parse_args()

    def crash(stage: str):
        if stage == args.crash_stage:
            report(args.report, {"crashed_at": stage})
            os._exit(73)

    try:
        manager = RecoveryManager(args.data_dir, fault_hook=crash)
        if args.crash_stage in {"sidecars_removed_unjournaled", "metadata_replaced_unjournaled"}:
            original_write_journal = manager._write_journal
            boundary = ("sidecars_removed" if args.crash_stage == "sidecars_removed_unjournaled"
                        else "metadata_installed")
            def before_journal(session, payload):
                if payload["stage"] == boundary:
                    crash(args.crash_stage)
                return original_write_journal(session, payload)
            manager._write_journal = before_journal
        if args.action == "resume":
            result = manager.resume()
        else:
            current = manager.inspect_current()
            backup = manager.inspect_backup(args.backup)
            result = manager.recover(
                args.backup,
                confirmed=True,
                expected_current=current["signature"],
                expected_backup=backup["signature"],
            )
        report(args.report, {"result": result})
        return 0
    except Exception as exc:
        report(args.report, {"error_type": type(exc).__name__,
                             "error_code": getattr(exc, "code", "")})
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
