"""Deploy-script env-var guards for ``tools/deploy_cf.py``.

Run with: python3 tests/test_deploy_cf.py

Purpose
-------
The dashboard Deploy modal exposes a ``--debug-log-bodies`` toggle. This test
pins the contract that backs it, offline and without calling gcloud:

* ``build_env_vars`` emits ``CF_DEBUG_LOG_BODIES=0`` by default and ``=1`` when
  the flag is on, while always carrying the base vars (LLM_BASE_URL,
  LOG_EXECUTION_ID, SUPABASE_URL);
* the parser leaves ``--env-vars`` unset by default so the flag can drive the
  env vars, and an explicit ``--env-vars`` still overrides it;
* ``build_deploy_cmd`` forwards ``args.env_vars`` into ``--set-env-vars``.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import deploy_cf  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    suffix = f"  ({detail})" if detail and not condition else ""
    print(f"  {status}  {name}{suffix}")
    if not condition:
        FAILURES.append(name)


def test_build_env_vars() -> None:
    print("\ntest_build_env_vars")
    off = deploy_cf.build_env_vars(False)
    on = deploy_cf.build_env_vars(True)
    check("default sets CF_DEBUG_LOG_BODIES=0", "CF_DEBUG_LOG_BODIES=0" in off)
    check("flag sets CF_DEBUG_LOG_BODIES=1", "CF_DEBUG_LOG_BODIES=1" in on)
    check("default is not also 1", "CF_DEBUG_LOG_BODIES=1" not in off)
    for name, value in (("off", off), ("on", on)):
        check(f"{name} carries LLM_BASE_URL", "LLM_BASE_URL=" in value)
        check(f"{name} carries LOG_EXECUTION_ID", "LOG_EXECUTION_ID=true" in value)
        check(f"{name} carries SUPABASE_URL", "SUPABASE_URL=" in value)


def test_parser_defaults() -> None:
    print("\ntest_parser_defaults")
    parser = deploy_cf.build_parser()
    base = parser.parse_args([])
    check("debug-log-bodies off by default", base.debug_log_bodies is False)
    check("env-vars unset by default (flag drives it)", base.env_vars is None)

    on = parser.parse_args(["--debug-log-bodies"])
    check("--debug-log-bodies sets the flag", on.debug_log_bodies is True)

    override = parser.parse_args(["--debug-log-bodies", "--env-vars", "FOO=bar"])
    check("explicit --env-vars is kept", override.env_vars == "FOO=bar")


def test_env_vars_reach_deploy_cmd() -> None:
    print("\ntest_env_vars_reach_deploy_cmd")
    args = argparse.Namespace(
        function="summarize", runtime=deploy_cf.DEFAULT_RUNTIME, entry_point=deploy_cf.DEFAULT_ENTRY_POINT,
        region=deploy_cf.DEFAULT_REGION, secret=deploy_cf.DEFAULT_SECRET,
        env_vars=deploy_cf.build_env_vars(True), timeout=3600, memory="1024Mi",
    )
    cmd = deploy_cf.build_deploy_cmd(args, "proj-123")
    check("--set-env-vars present", "--set-env-vars" in cmd)
    value = cmd[cmd.index("--set-env-vars") + 1]
    check("debug flag forwarded into env vars", "CF_DEBUG_LOG_BODIES=1" in value)
    check("project forwarded", cmd[cmd.index("--project") + 1] == "proj-123")


def main() -> None:
    print(f"deploy_cf env-var guard ({ROOT})")
    test_build_env_vars()
    test_parser_defaults()
    test_env_vars_reach_deploy_cmd()

    if FAILURES:
        print(f"\n{len(FAILURES)} GUARD CHECK(S) FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("\nALL DEPLOY ENV-VAR GUARD CHECKS PASSED")


if __name__ == "__main__":
    main()
