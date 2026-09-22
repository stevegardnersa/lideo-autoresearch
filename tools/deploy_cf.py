#!/usr/bin/env python3
"""Deploy the staged prompt bundle to the Cloud Function.

Stages ``cloud_function/prompts/registry.json`` (via ``tools/sync_prompts.py``),
verifies the bundle matches ``data/prompts/registry.json``, runs
``gcloud functions deploy``, then probes the deployed function with an
authenticated zero-cost scoring request and records the result to
``data/prompts/cf_last_report.json``.

The deploy is destructive to production, so it requires ``--confirm`` (or
``--dry-run``). Drift between the local registry and the bundle aborts unless
``--no-sync`` is allowed to be overridden with ``--allow-drift``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import prompt_registry  # noqa: E402

BUNDLE_PATH = ROOT / "cloud_function" / "prompts" / "registry.json"
SYNC_SCRIPT = ROOT / "tools" / "sync_prompts.py"
DEFAULT_MEMORY = "1024Mi"
DEFAULT_TIMEOUT = "3600"
DEFAULT_REGION = "us-central1"
DEFAULT_FUNCTION = "summarize"
DEFAULT_ENTRY_POINT = "summarize"
DEFAULT_RUNTIME = "python312"
# The CF resolves chapter `source_ref` objects from Supabase Storage, so the
# deploy must carry both the project URL and the service-role secret. Override
# the URL with the SUPABASE_URL env var for a different project.
DEFAULT_SUPABASE_URL = "https://apnsbsxifvwaxrlfklko.supabase.co"
DEFAULT_SECRET = "LLM_API_KEY=openrouter-api-key:latest,SUPABASE_SERVICE_ROLE_KEY=supabase-service-role:latest"
DEFAULT_ENV_VARS = (
    "LLM_BASE_URL=https://openrouter.ai/api/v1,LOG_EXECUTION_ID=true,"
    f"SUPABASE_URL={os.environ.get('SUPABASE_URL', DEFAULT_SUPABASE_URL)}"
)

PROBE_PAYLOAD: Dict[str, Any] = {
    "source_md": "## Intro\n\nFeedback loops matter.",
    "model": "test/deploy-probe",
    "summary_md": "## Summary\n\nFeedback loops matter.",
    "target_words": 10,
    "score": True,
    "rubric": {"core_concepts": ["feedback loops"], "key_terms": ["feedback"]},
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def which_gcloud() -> str:
    found = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not found:
        raise SystemExit("ERROR: gcloud not found on PATH. Install the Google Cloud SDK first.")
    return found


def read_bundle() -> Dict[str, Any]:
    if not BUNDLE_PATH.exists():
        return {}
    try:
        return json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def bundle_sha() -> str:
    return str(read_bundle().get("registry_sha") or "")


def local_registry() -> Dict[str, Any]:
    return prompt_registry.load_registry(prompt_registry.registry_path(), seed=True)


def resolve_project(explicit: str) -> str:
    if explicit:
        return explicit

    for key in ("PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    proc = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True)
    val = (proc.stdout or "").strip()
    if val and val != "(unset)":
        return val
    raise SystemExit(
        "ERROR: no GCP project. Pass --project, set PROJECT_ID, or run "
        "`gcloud config set project <id>`."
    )


def run_sync() -> int:
    _log(f"$ {sys.executable} {SYNC_SCRIPT.relative_to(ROOT)}")
    return subprocess.run([sys.executable, str(SYNC_SCRIPT)], cwd=ROOT).returncode


def production_prompt_meta(registry: Dict[str, Any], stage: str = "chapter") -> Tuple[str, str]:
    slots = (registry.get("slots") or {}).get(stage) or {}
    version_id = str(((slots.get("general") or {}).get("production")) or "")
    version = prompt_registry.get_version(version_id, prompt_registry.registry_path()) if version_id else None
    fingerprint = str((version or {}).get("system_prompt_fingerprint") or "")
    return version_id, fingerprint


def build_deploy_cmd(args: argparse.Namespace, project: str) -> List[str]:
    return [
        "gcloud", "functions", "deploy", args.function,
        "--gen2",
        "--runtime", args.runtime,
        "--trigger-http",
        "--entry-point", args.entry_point,
        "--source", str(ROOT),
        "--region", args.region,
        "--project", project,
        "--set-secrets", args.secret,
        "--set-env-vars", args.env_vars,
        "--timeout", str(args.timeout),
        "--memory", args.memory,
        "--no-allow-unauthenticated",
    ]


def describe_function(project: str, region: str, function: str) -> Dict[str, str]:
    proc = subprocess.run(
        [
            "gcloud", "functions", "describe", function,
            "--gen2", "--region", region, "--project", project, "--format", "json",
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gcloud describe failed").strip())
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    svc = data.get("serviceConfig") or {}
    return {
        "url": str(svc.get("uri") or ""),
        "revision": str(svc.get("revision") or ""),
        "updated": str(svc.get("updateTime") or ""),
    }


def identity_token() -> str:
    proc = subprocess.run(["gcloud", "auth", "print-identity-token"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "no identity token").strip())
    token = (proc.stdout or "").strip()
    if not token:
        raise RuntimeError("gcloud returned an empty identity token")
    return token


def smoke_probe(url: str, token: str, timeout: int = 180) -> Dict[str, Any]:
    payload = json.dumps(PROBE_PAYLOAD).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return {"ok": resp.status == 200 and bool(body.get("success")), "status": resp.status, "meta": (body.get("meta") or {})}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": raw[:500]}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


def write_report(report: Dict[str, Any]) -> None:
    existing = prompt_registry.read_cf_report()
    existing.update(report)
    try:
        prompt_registry.write_cf_report(existing)
        _log(f"wrote {prompt_registry.cf_report_path().relative_to(ROOT)}")
    except OSError as exc:
        _log(f"WARN: could not write cf report: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="", help="GCP project id (default: env or gcloud config).")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--function", default=DEFAULT_FUNCTION)
    parser.add_argument("--entry-point", default=DEFAULT_ENTRY_POINT)
    parser.add_argument("--runtime", default=DEFAULT_RUNTIME)
    parser.add_argument("--memory", default=DEFAULT_MEMORY)
    parser.add_argument("--timeout", type=int, default=int(DEFAULT_TIMEOUT))
    parser.add_argument("--secret", default=DEFAULT_SECRET, help="--set-secrets value.")
    parser.add_argument("--env-vars", default=DEFAULT_ENV_VARS, help="--set-env-vars value.")
    parser.add_argument("--no-sync", action="store_true", help="Deploy the existing bundle without syncing.")
    parser.add_argument("--allow-drift", action="store_true", help="Deploy even if the bundle differs from the local registry.")
    parser.add_argument("--no-smoke", action="store_true", help="Skip the post-deploy smoke probe.")
    parser.add_argument("--dry-run", action="store_true", help="Print the deploy command without running it.")
    parser.add_argument("--confirm", action="store_true", help="Required to actually deploy.")
    args = parser.parse_args()

    if not args.dry_run and not args.confirm:
        _log("ERROR: refusing to deploy without --confirm (or pass --dry-run).")
        return 2

    if not args.no_sync:
        rc = run_sync()
        if rc != 0:
            _log("ERROR: sync failed; not deploying.")
            return 2

    registry = local_registry()
    local_sha = str(registry.get("registry_sha") or "")
    staged_sha = bundle_sha()

    if staged_sha != local_sha:
        if not args.allow_drift:
            _log(
                "ERROR: bundle drift — cloud_function/prompts/registry.json does not match "
                f"the local registry ({staged_sha[:8] or 'none'} != {local_sha[:8] or 'none'}). "
                "Re-run without --no-sync, or pass --allow-drift to override."
            )
            write_report({"ok": False, "error": "bundle_drift", "registry_sha": staged_sha})
            return 2
        _log(f"WARN: deploying drifted bundle {staged_sha[:8] or 'none'} (local {local_sha[:8] or 'none'}).")

    which_gcloud()
    project = resolve_project(args.project)
    cmd = build_deploy_cmd(args, project)

    _log(f"project={project} region={args.region} function={args.function} registry_sha={staged_sha[:8] or 'none'}")

    if args.dry_run:
        _log("dry-run: " + " ".join(cmd))
        return 0

    _log("$ " + " ".join(cmd))
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    if rc != 0:
        _log(f"ERROR: gcloud deploy failed (exit {rc}).")
        write_report({"ok": False, "error": "deploy_failed", "registry_sha": staged_sha, "project": project, "region": args.region, "function": args.function})
        return 1

    info: Dict[str, str] = {}
    try:
        info = describe_function(project, args.region, args.function)
        _log(f"deployed url={info.get('url')} revision={info.get('revision') or info.get('updated')}")
    except Exception as exc:
        _log(f"WARN: could not describe function: {exc}")

    report: Dict[str, Any] = {
        "ok": True,
        "deployed": True,
        "registry_sha": staged_sha,
        "project": project,
        "region": args.region,
        "function": args.function,
        "url": info.get("url", ""),
        "revision": info.get("revision", "") or info.get("updated", ""),
    }

    if args.no_smoke:
        _log("smoke: skipped (--no-smoke)")
        write_report(report)
        return 0

    probe_ok = False
    try:
        token = identity_token()
        result = smoke_probe(info.get("url", ""), token)
    except Exception as exc:
        result = {"ok": False, "status": 0, "error": str(exc)}
    report["smoke"] = result
    probe_sha = str((result.get("meta") or {}).get("prompt_registry_sha") or "")
    report["probe_registry_sha"] = probe_sha

    if result.get("ok") and (not staged_sha or probe_sha == staged_sha):
        probe_ok = True
        version_id, fingerprint = production_prompt_meta(registry, "chapter")
        report.update({
            "prompt_version": version_id,
            "prompt_fingerprint": fingerprint,
            "prompt_source": "deploy_smoke",
            "prompt_model": "",
        })
        _log(f"smoke: ok (status {result.get('status')}, registry_sha {probe_sha[:8] or 'n/a'})")
    else:
        report["ok"] = False
        detail = result.get("error") or f"registry_sha mismatch (probe {probe_sha[:8] or 'none'} != {staged_sha[:8] or 'none'})"
        _log(f"smoke: FAILED (status {result.get('status')}) {detail}")
        _log("WARN: keeping previous prompt_* report fields; deploy itself succeeded.")

    write_report(report)
    return 0 if probe_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
