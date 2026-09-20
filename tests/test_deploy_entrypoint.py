"""Deployment entry-point drift guard for the `summarize` Cloud Function.

Run with: python3 tests/test_deploy_entrypoint.py

Purpose
-------
The Cloud Function is deployed from the repository ROOT with
`--entry-point=summarize` (the function exposed by `cloud_function/main.py`,
re-exported by the root `main.py` shim). The root shim was chosen as the
deployment source in place of the old `cloud_function/` upload so that bundling
picks up the vendored `core/` package and the bundled prompt registry.

This test is AST-only and offline. It guards against silent drift:

* the entry point (`root main.py` -> `cloud_function/main.py` -> a
  `@functions_framework.http summarize`) stays consistent across refactors;
* `.gcloudignore` never lets docs/specs/keys or agent session data into the
  deploy bundle (the function bundle must NOT contain `specs/`, `*.md` or
  `secrets/`);
* a stale `cloud_function/`-style SelfSustainedBundle/`request_bundle` copy or
  an unresolved `functions_framework` import cannot be reintroduced unseen.

See specs/nextjs-cloud-function-production-prompt-concurrent-runs.md §16 and
.claude/skills/cloud-function-deploy/SKILL.md for the deployment contract.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_MAIN = ROOT / "main.py"
CF_MAIN = ROOT / "cloud_function" / "main.py"
GCLOUD_IGNORE = ROOT / ".gcloudignore"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    suffix = f"  ({detail})" if detail and not condition else ""
    print(f"  {status}  {name}{suffix}")
    if not condition:
        FAILURES.append(name)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def decorator_attributes(node: ast.FunctionDef) -> set[tuple[str, ...]]:
    attrs: set[tuple[str, ...]] = set()
    for deco in node.decorator_list:
        if isinstance(deco, ast.Attribute) and isinstance(deco.value, ast.Name):
            attrs.add((deco.value.id, deco.attr))
    return attrs


def test_root_shim_re_exports_summarize() -> None:
    print("\nteast_root_shim_re_exports_summarize")
    check("root main.py exists", ROOT_MAIN.exists())
    if not ROOT_MAIN.exists():
        return

    tree = parse(ROOT_MAIN)
    has_importer = any(
        isinstance(n, ast.ImportFrom) and n.module == "cloud_function.main"
        for n in tree.body
    )
    check("shim imports from cloud_function.main", has_importer)

    __all__ = next(
        (
            n.value.elts
            for n in tree.body
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "__all__"
        ),
        None,
    )
    check("__all__ re-exports 'summarize'", __all__ is not None and any(
        isinstance(e, ast.Constant) and e.value == "summarize" for e in (__all__ or [])
    ))


def test_cloud_function_main_exposes_summarize() -> None:
    print("\ntest_cloud_function_main_exposes_summarize")
    check("cloud_function/main.py exists", CF_MAIN.exists())
    if not CF_MAIN.exists():
        return

    tree = parse(CF_MAIN)
    summarize = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "summarize"),
        None,
    )
    check("cloud_function/main.py defines summarize()", summarize is not None)
    if summarize is None:
        return

    decorators = decorator_attributes(summarize)
    check(
        "summarize is wired with @functions_framework.http",
        ("functions_framework", "http") in decorators,
        "expected @functions_framework.http decorator",
    )

    # must route request handling to cloud_function.handler, never inline the
    # whole pipeline (that would let refactors bypass the bundled core).
    handler_import = any(
        isinstance(n, ast.ImportFrom) and n.module in ("cloud_function.handler", "cloud_function.handler")
        for n in tree.body
    )
    check("handler pipeline imported (run_summarize/build_response)", handler_import)


def test_gcloudignore_keeps_bundle_prod_only() -> None:
    print("\ntest_gcloudignore_keeps_bundle_prod_only")
    check(".gcloudignore exists", GCLOUD_IGNORE.exists())
    if not GCLOUD_IGNORE.exists():
        return

    text = GCLOUD_IGNORE.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]

    patterns = {
        "docs (*.md) excluded": r"\*\.md$",
        "specs/ excluded": r"^specs/?",
        "tests/ excluded": r"^tests/?",
        "secrets/ excluded": r"^secrets/?",
        "agent session data (.lcm/ .opencode/) excluded": r"^\.(lcm|opencode|openchamber|sessions)/",
        "VCS ignored": r"^\.git/?",
        "local venv ignored": r"^\.venv/?",
    }
    for label, pattern in patterns.items():
        check(label, any(re.search(pattern, ln) for ln in lines), f"expected pattern {pattern!r}")

    # the entry point and its constituents must never be ignored
    not_ignored = (
        r"^(main|cloud_function/main|cloud_function/handler|cloud_function/prompts/registry)\.py$",
    )
    for pattern in not_ignored:
        check(
            f"entry point modules not ignored ({pattern})",
            not any(re.search(pattern, ln) for ln in lines),
            f"pattern {pattern!r} must not appear in .gcloudignore",
        )


def test_no_stale_duplicate_entrypoints() -> None:
    print("\ntest_no_stale_duplicate_entrypoints")
    # a leftover cloud_function/... or app.py upload target that shadows the
    # root shim would change what gen2 deploys.
    stale_candidates = [p for p in ROOT.rglob("*") if p.is_file() and p.name in {"app.py", "server.py"}]
    stale_candidates = [p for p in stale_candidates if ".venv" not in p.parts]
    check("no app.py/server.py shadow entry points", not stale_candidates,
          ", ".join(str(p) for p in stale_candidates))

    # exactly one module named "main" exports the function at deployment time.
    mains = [p for p in (ROOT / "cloud_function").glob("main.py")]
    check("single cloud_function/main.py", len(mains) == 1)


def main() -> None:
    print(f"Deployment entry-point guard ({ROOT})")
    test_root_shim_re_exports_summarize()
    test_cloud_function_main_exposes_summarize()
    test_gcloudignore_keeps_bundle_prod_only()
    test_no_stale_duplicate_entrypoints()

    if FAILURES:
        print(f"\n{len(FAILURES)} GUARD CHECK(S) FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("\nALL DEPLOY ENTRY-POINT GUARD CHECKS PASSED")


if __name__ == "__main__":
    main()