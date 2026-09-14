"""Offline tests for tools.add_candidate.remove_run_cascade.

Run with: python3 tests/test_remove_run.py

Covers the single-run delete path used by the dashboard Runs manager:
  * the base run id matches the whole family (JSON + state + samples + llmj sidecars)
  * results.tsv rows for the family (base and sidecar share the run id) are dropped
  * dry-run keeps every file untouched and still reports the plan
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.add_candidate import remove_run_cascade

ROOT = Path(__file__).resolve().parent.parent


RUN_ID = "20260501t000000z__booksum-v4__chapter_fast-v3__30m_nebula_thinking_v1"


def _setup(tmp_path: Path):
    runs = tmp_path / "runs"
    bench = runs / "booksum-v4"
    bench.mkdir(parents=True, exist_ok=True)
    artifacts = [
        f"{RUN_ID}.json",
        f"{RUN_ID}.state.json",
        f"{RUN_ID}.samples.jsonl",
        f"{RUN_ID}__llmj_gpt-5.4-mini.json",
        f"{RUN_ID}__llmj_gpt-5.4-mini.state.json",
        f"{RUN_ID}__llmj_gpt-5.4-mini.samples.jsonl",
        # a neighbouring family that must survive untouched
        "20260502t000000z__booksum-v4__chapter_fast-v3__30m_nebula_thinking_v2.json",
        "20260502t000000z__booksum-v4__chapter_fast-v3__30m_nebula_thinking_v2.samples.jsonl",
    ]
    for name in artifacts:
        (bench / name).write_text("{}", encoding="utf-8")

    tsv = tmp_path / "results.tsv"
    header = "timestamp\tbaz_run_id\tmean_quality"
    tsv.write_text(
        header
        + "\n"
        + f"2026-05-01T00:00:00Z\t{RUN_ID}\t0.7\n"
        + f"2026-05-01T00:01:00Z\t{RUN_ID}\t0.8\n"
        + f"2026-05-02T00:00:00Z\t20260502t000000z__booksum-v4__chapter_fast-v3__30m_nebula_thinking_v2\t0.6\n",
        encoding="utf-8",
    )
    return runs, tsv


def _run_id_present(tmp_path: Path, run_id: str) -> bool:
    return run_id in (tmp_path / "results.tsv").read_text(encoding="utf-8")


def test_remove_run_cascade_finds_full_family(tmp_path):
    runs, tsv = _setup(tmp_path)
    summary = remove_run_cascade(RUN_ID, runs_dir=runs, jobs_dir=tmp_path / "jobs", results_tsv=tsv, dry_run=True)
    assert summary["run_id"] == RUN_ID
    names = [Path(p).name for p in summary["run_files"]]
    assert len(summary["run_files"]) == 6, names
    for suffix in [
        ".json",
        ".state.json",
        ".samples.jsonl",
        "__llmj_gpt-5.4-mini.json",
        "__llmj_gpt-5.4-mini.state.json",
        "__llmj_gpt-5.4-mini.samples.jsonl",
    ]:
        assert any(n == RUN_ID + suffix for n in names), f"missing {suffix}"
    assert all(Path(p).exists() for p in summary["run_files"]), "dry-run must not delete files"
    assert summary["result_rows"] == 2
    assert _run_id_present(tmp_path, RUN_ID), "dry-run keeps results.tsv rows"


def test_remove_run_cascade_deletes_files_and_rows(tmp_path):
    runs, tsv = _setup(tmp_path)
    summary = remove_run_cascade(RUN_ID, runs_dir=runs, jobs_dir=tmp_path / "jobs", results_tsv=tsv, dry_run=False)
    assert len(summary["run_files"]) == 6
    assert summary["result_rows"] == 2
    for p in summary["run_files"]:
        assert not Path(p).exists(), f"expected deleted: {p}"

    bench = runs / "booksum-v4"
    surviving = [n for n in bench.iterdir()]
    assert len(surviving) == 2, [s.name for s in surviving]
    assert not _run_id_present(tmp_path, RUN_ID), "family rows removed from results.tsv"
    assert _run_id_present(tmp_path, "20260502t000000z__booksum-v4__chapter_fast-v3__30m_nebula_thinking_v2")
    # results.tsv keeps its header + the surviving row
    lines = (tmp_path / "results.tsv").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("timestamp")


def test_remove_run_cascade_unknown_id_reports_no_match(tmp_path):
    runs, tsv = _setup(tmp_path)
    summary = remove_run_cascade("20260503t000000z__does_not_exist_v1", runs_dir=runs, jobs_dir=tmp_path / "jobs", results_tsv=tsv)
    assert summary["run_files"] == []
    assert summary["result_rows"] == 0
    assert _run_id_present(tmp_path, RUN_ID), "nothing removed"


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        test_remove_run_cascade_finds_full_family(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_remove_run_cascade_deletes_files_and_rows(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_remove_run_cascade_unknown_id_reports_no_match(Path(d))
    print("ALL REMOVE-RUN TESTS PASSED")


if __name__ == "__main__":
    main()