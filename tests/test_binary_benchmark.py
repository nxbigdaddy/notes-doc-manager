import pytest

from scripts import benchmark_binary_heuristic as benchmark


@pytest.mark.parametrize("characters,rounds", [(0, 1), (True, 1), (1048577, 1), (200, 0), (200, 10)])
def test_benchmark_has_bounded_inputs(characters, rounds):
    with pytest.raises(ValueError):
        benchmark.run_benchmark(characters=characters, rounds=rounds)


def test_small_benchmark_proves_results_and_records_samples_not_performance_thresholds():
    result = benchmark.run_benchmark(characters=200, rounds=1)
    assert result["measurement_valid"] and result["semantics_equal"]
    assert len(result["cases"]) == 8
    cases = {item["name"]: item for item in result["cases"]}
    assert not cases["exact_two_percent_tail"]["binary"]
    assert cases["over_two_percent_tail"]["binary"] and cases["nul_tail"]["binary"]
    for item in result["cases"]:
        assert len(item["samples_ns"]["reference"]) == len(item["samples_ns"]["candidate"]) == 1
        assert item["reference_median_ms"] >= 0 and item["candidate_median_ms"] >= 0


def test_invalid_candidate_never_produces_accepted_performance_report(monkeypatch):
    monkeypatch.setattr(benchmark.files, "_looks_binary", lambda _: False)
    with pytest.raises(AssertionError):
        benchmark.run_benchmark(characters=200, rounds=1)
