"""Bounded unprofiled timings of synthetic decoded-text classification only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import platform
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from notes.infrastructure import files  # noqa: E402

MAX_CHARACTERS = 1024 * 1024


def reference(text):
    """Frozen 0.8.1 expression, independent of the production helper."""
    return bool("\x00" in text or (
        text and sum(ord(character) < 32 and character not in "\t\n\r\f"
                     for character in text) / len(text) > 0.02
    ))


def corpus(characters):
    exact = characters // 50 * 50
    count = exact // 50

    def repeat(value):
        return (value * (characters // len(value) + 1))[:characters]

    return {
        "ascii_clean": "A" * characters,
        "unicode_clean": repeat("中文😀 e\u0301 · 文档 "),
        "allowed_whitespace": repeat("alpha\tbeta\n gamma\r\n\f"),
        "del_c1_preserved": repeat("text\x7f\x85\x9f"),
        "exact_two_percent_tail": "A" * (exact - count) + "\x01" * count,
        "over_two_percent_tail": "A" * (exact - count - 1) + "\x01" * (count + 1),
        "nul_tail": "A" * (characters - 1) + "\x00",
        "dense_control": "\x01" * characters,
    }


def run_benchmark(*, characters=256 * 1024, rounds=5):
    if (type(characters) is not int or not 200 <= characters <= MAX_CHARACTERS
            or type(rounds) is not int or not 1 <= rounds <= 9):
        raise ValueError("Use 200..1048576 characters and 1..9 rounds")
    candidate = getattr(files, "_looks_binary", None)
    if not callable(candidate):
        raise ValueError("The production binary heuristic helper is not available")
    source = Path(files.__file__).resolve()
    source_before = hashlib.sha256(source.read_bytes()).hexdigest()
    cases = []
    for name, text in corpus(characters).items():
        expected = reference(text)
        actual = candidate(text)
        if type(actual) is not bool or actual is not expected:
            raise AssertionError(name)
        samples = {"reference": [], "candidate": []}
        for number in range(rounds):
            order = ("reference", "candidate") if number % 2 == 0 else ("candidate", "reference")
            for label in order:
                function = reference if label == "reference" else candidate
                started = time.perf_counter_ns()
                actual = function(text)
                samples[label].append(time.perf_counter_ns() - started)
                if type(actual) is not bool or actual is not expected:
                    raise AssertionError((name, label))
        before = statistics.median(samples["reference"])
        after = statistics.median(samples["candidate"])
        cases.append({
            "name": name, "characters": len(text), "utf8_bytes": len(text.encode("utf-8")),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "binary": expected, "samples_ns": samples,
            "reference_median_ms": before / 1_000_000, "candidate_median_ms": after / 1_000_000,
            "reference_over_candidate": before / after if after else None,
        })
    source_after = hashlib.sha256(source.read_bytes()).hexdigest()
    return {
        "format_version": 1, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "synthetic decoded-string heuristic only; no file IO or SQL in timed calls",
        "python": sys.version, "platform": platform.platform(),
        "requested_characters": characters, "rounds": rounds,
        "source_before": source_before, "source_after": source_after,
        "reference_sha256": hashlib.sha256(inspect.getsource(reference).encode("utf-8")).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "measurement_valid": source_before == source_after, "semantics_equal": True,
        "cases": cases,
        "limitations": [
            "This script does not enable a profiler. Alternating warmed calls reduce order bias.",
            "One local experiment, not p95, cold-cache latency or ApplicationService throughput.",
            "No inference about a 10000-document/500MiB library or GUI responsiveness.",
            "Dense-control early exit is logical proof of exceeding the original full-length threshold, not sampling.",
        ],
    }


def write_report(report):
    directory = ROOT / ".artifacts"
    for component in (*reversed(directory.parents), directory):
        if component.exists() and (component.is_symlink()
                                  or getattr(component.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Artifact directory cannot traverse a link/reparse point")
    directory.mkdir(exist_ok=True)
    if directory.resolve().parent != ROOT:
        raise ValueError("Artifact directory escaped project")
    path = directory / ("binary-heuristic-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                        + "-" + uuid.uuid4().hex[:12] + ".json")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--characters", type=int, default=256 * 1024)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    report = run_benchmark(characters=args.characters, rounds=args.rounds)
    path = write_report(report)
    print(f"Report: {path}", flush=True)
    for case in report["cases"]:
        print(f"{case['name']}: old {case['reference_median_ms']:.6f}ms, "
              f"candidate {case['candidate_median_ms']:.6f}ms", flush=True)
    return 0 if report["measurement_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
