#!/usr/bin/env python3
"""Reset benchmark: clear runs, results, candidates snapshot, and PROFILE_CANDIDATES to start fresh."""

import shutil
import sys
from pathlib import Path

BOOK_GATE = Path("bench/book_gate.jsonl")

ARTIFACTS_RUNS = Path("artifacts/runs")
RESULTS_TSV = Path("results.tsv")
CANDIDATES_JSON = Path("data/candidates.json")
SNAPSHOTS_CATALOG = Path("snapshots/catalog")
SNAPSHOTS_PRICING = Path("snapshots/pricing")
CANDIDATE_SPEC = Path("candidate_spec.py")


def find_matching_brace(content: str, start: int) -> int:
    """Find the closing brace that matches the opening brace at position start."""
    depth = 1
    i = start + 1
    while i < len(content) and depth > 0:
        c = content[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        i += 1
    return i - 1


def clear_profile_candidates(path: Path) -> bool:
    with open(path, "r") as f:
        content = f.read()

    marker = "PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = {"
    if marker not in content:
        print(f"  ERROR: Could not find '{marker}' in {path}")
        return False

    start = content.index(marker) + len(marker)
    open_brace = start
    close_brace = find_matching_brace(content, open_brace)

    new_content = content[:open_brace] + "}" + content[close_brace + 1:]

    with open(path, "w") as f:
        f.write(new_content)
    return True


def clear_profile_literal(path: Path) -> bool:
    with open(path, "r") as f:
        content = f.read()

    marker = "Profile = Literal["
    if marker not in content:
        print(f"  ERROR: Could not find '{marker}' in {path}")
        return False

    start = content.index(marker) + len(marker)
    close_bracket = content.index("]", start)

    new_content = content[:start] + '"none"]' + content[close_bracket + 1:]

    with open(path, "w") as f:
        f.write(new_content)
    return True


def confirm(msg: str) -> bool:
    response = input(f"{msg} [y/N] ").strip().lower()
    return response in ("y", "yes")


def dry_run_report(paths: list, action: str):
    for p in paths:
        if isinstance(p, tuple):
            path, desc = p
        else:
            path, desc = p, str(p)
        if path.exists():
            if path.is_dir():
                size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            else:
                size = path.stat().st_size
            print(f"  {'would delete' if action == 'dry' else 'deleting'}: {desc} ({size:,} bytes)")
        else:
            print(f"  skipping (not found): {desc}")


def main():
    print("=== Benchmark Reset Script ===\n")

    paths_to_clear = [
        (ARTIFACTS_RUNS, "artifacts/runs"),
        (RESULTS_TSV, "results.tsv"),
        (CANDIDATES_JSON, "data/candidates.json"),
        (BOOK_GATE, "bench/book_gate.jsonl"),
    ]

    snapshot_dirs = [SNAPSHOTS_CATALOG, SNAPSHOTS_PRICING]

    print("Files/dirs that would be cleared:")
    dry_run_report(paths_to_clear, "dry")
    for sd in snapshot_dirs:
        if sd.exists():
            for f in sorted(sd.glob("*")):
                print(f"  would delete snapshot: {f}")

    print(f"  would clear Profile Literal[] in {CANDIDATE_SPEC}")
    print(f"  would clear PROFILE_CANDIDATES in {CANDIDATE_SPEC}")

    print()
    if not confirm("Proceed with deletion?"):
        print("Aborted.")
        sys.exit(1)

    print("\nDeleting...")
    for item in paths_to_clear:
        path = item[0] if isinstance(item, tuple) else item
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            print(f"  deleted: {path}")
        else:
            print(f"  skipped (not found): {path}")

    for sd in snapshot_dirs:
        if sd.exists():
            for f in sorted(sd.glob("*")):
                f.unlink()
                print(f"  deleted snapshot: {f}")

    print("\nClearing Profile Literal[] and PROFILE_CANDIDATES...")
    if clear_profile_literal(CANDIDATE_SPEC):
        print(f"  cleared Profile Literal[]: {CANDIDATE_SPEC}")
    if clear_profile_candidates(CANDIDATE_SPEC):
        print(f"  cleared PROFILE_CANDIDATES: {CANDIDATE_SPEC}")

    print("\nDone. Run 'python run.py' to start a fresh benchmark.")


if __name__ == "__main__":
    main()