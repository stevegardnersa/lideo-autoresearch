"""Generate per-model markdown optimization reports.

The reporter reads optimization run JSON output from the agent and
produces human-readable markdown summaries suitable for review.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class RunSummary:
    base_variant: str
    model: str
    time_budget: str
    signal_count: int = 0
    dimensions_with_notes: List[str] = None
    baseline_composite: float = 0.0
    baseline_quality: float = 0.0
    baseline_faithfulness: float = 0.0
    baseline_concept: float = 0.0
    baseline_pass: float = 0.0
    best_composite: float = 0.0
    best_quality: float = 0.0
    best_faithfulness: float = 0.0
    best_concept: float = 0.0
    best_pass: float = 0.0
    best_variant_name: str = ""
    best_changes: List[str] = None
    variants_tested: int = 0
    improved: bool = False

    def __post_init__(self):
        if self.dimensions_with_notes is None:
            self.dimensions_with_notes = []
        if self.best_changes is None:
            self.best_changes = []
        self.improved = self.best_composite > self.baseline_composite


def _delta_str(best: float, base: float, fmt: str = ".3f") -> str:
    diff = best - base
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:{fmt}}"


def render_run_markdown(run: RunSummary) -> str:
    lines: List[str] = []
    lines.append(f"## {run.base_variant}\n")
    lines.append(f"**Model:** {run.model}  ")
    lines.append(f"**Budget:** {run.time_budget}  ")
    lines.append(f"**Notes received:** {run.signal_count}  ")
    lines.append(f"**Dimensions:** {', '.join(run.dimensions_with_notes) if run.dimensions_with_notes else 'none'}\n")

    lines.append("### Baseline\n")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Composite | {run.baseline_composite:.4f} |")
    lines.append(f"| Quality | {run.baseline_quality:.2f} |")
    lines.append(f"| Faithfulness | {run.baseline_faithfulness:.2f} |")
    lines.append(f"| Concept Coverage | {run.baseline_concept:.2f} |")
    lines.append(f"| Pass Rate | {run.baseline_pass:.1%} |\n")

    if run.best_variant_name:
        lines.append("### Best Variant\n")
        lines.append(f"**Name:** `{run.best_variant_name}`  ")
        lines.append(f"**Changes:** {', '.join(run.best_changes) if run.best_changes else 'none'}\n")
        lines.append(f"| Metric | Best | Delta |")
        lines.append(f"|--------|------|-------|")
        lines.append(f"| Composite | {run.best_composite:.4f} | {_delta_str(run.best_composite, run.baseline_composite)} |")
        lines.append(f"| Quality | {run.best_quality:.2f} | {_delta_str(run.best_quality, run.baseline_quality)} |")
        lines.append(f"| Faithfulness | {run.best_faithfulness:.2f} | {_delta_str(run.best_faithfulness, run.baseline_faithfulness)} |")
        lines.append(f"| Concept Coverage | {run.best_concept:.2f} | {_delta_str(run.best_concept, run.baseline_concept)} |")
        lines.append(f"| Pass Rate | {run.best_pass:.1%} | {_delta_str(run.best_pass, run.baseline_pass)} |\n")

        if run.improved:
            lines.append("**Result:** Improved \u2705\n")
        else:
            lines.append("**Result:** No improvement \u274c\n")
    else:
        lines.append("### Result: No variant found\n")

    lines.append(f"**Variants tested:** {run.variants_tested}  ")
    lines.append("---\n")
    return "\n".join(lines)


def report_from_agent_output(
    agent_output_path: str,
    output_path: Optional[str] = None,
) -> str:
    """Generate a markdown report from the agent's JSON output.

    Args:
        agent_output_path: Path to the JSON file produced by agent.py --output
        output_path: If given, write markdown to this file. Otherwise return as string.

    Returns the markdown string.
    """
    with open(agent_output_path, "r") as f:
        data = json.load(f)

    runs = data.get("runs", [])
    summaries: List[RunSummary] = []

    for r in runs:
        summary = RunSummary(
            base_variant=r.get("base_variant", "?"),
            model=r.get("model", "?"),
            time_budget=r.get("time_budget", "?"),
            signal_count=r.get("signal_count", 0),
            dimensions_with_notes=r.get("dimensions_with_notes", []),
            baseline_composite=r.get("baseline_composite", 0),
            baseline_quality=r.get("baseline_quality", 0),
            baseline_faithfulness=r.get("baseline_faithfulness", 0),
            baseline_concept=r.get("baseline_concept_coverage", 0),
            baseline_pass=r.get("baseline_pass_rate", 0),
            best_composite=r.get("best_composite", 0),
            best_quality=r.get("best_quality", 0),
            best_faithfulness=r.get("best_faithfulness", 0),
            best_concept=r.get("best_concept_coverage", 0),
            best_pass=r.get("best_pass_rate", 0),
            best_variant_name=r.get("best_variant_name", "") or "",
            best_changes=r.get("best_changes", []),
            variants_tested=r.get("steps", 0),
        )
        summaries.append(summary)

    lines: List[str] = []
    lines.append(f"# Autoresearch Optimization Report\n")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"Notes file: {data.get('notes_file', '?')}  ")
    lines.append(f"Total signals: {data.get('total_signals', 0)}  ")
    lines.append(f"Candidates evaluated: {len(summaries)}\n")
    lines.append("---\n")

    if summaries:
        # Sort by improvement delta
        summaries.sort(
            key=lambda s: s.best_composite - s.baseline_composite,
            reverse=True,
        )

        # Summary table
        lines.append("## At a Glance\n")
        lines.append("| Candidate | Baseline | Best | Delta | Improved |")
        lines.append("|-----------|----------|------|-------|----------|")
        for s in summaries:
            delta = s.best_composite - s.baseline_composite
            improved = "Yes" if s.improved else "No"
            lines.append(
                f"| {s.base_variant} | {s.baseline_composite:.4f} | "
                f"{s.best_composite:.4f} | {delta:+.4f} | {improved} |"
            )
        lines.append("")

        for s in summaries:
            lines.append(render_run_markdown(s))

    report = "\n".join(lines)
    if output_path:
        with open(output_path, "w") as f:
            f.write(report)
        print(f"Report written to {output_path}")
    return report


def report_from_optimization_runs(
    runs: List[OptimizationRun],
    signals: Signals,
    output_path: Optional[str] = None,
) -> str:
    """Generate a markdown report directly from in-memory optimization runs."""
    summaries: List[RunSummary] = []
    for opt_run in runs:
        csig = signals.candidates.get(opt_run.base_variant_name)
        signal_count = csig.total_signals if csig else 0
        dims = list(csig.dimensions.keys()) if csig else []

        baseline = opt_run.best_result  # First result in steps is baseline
        # Find baseline step (first step evaluated)
        baseline_result = opt_run.steps[0].result if opt_run.steps else None

        best_result = opt_run.best_result
        best_variant = opt_run.best_variant

        summary = RunSummary(
            base_variant=opt_run.base_variant_name,
            model=opt_run.model_name,
            time_budget=opt_run.time_budget,
            signal_count=signal_count,
            dimensions_with_notes=dims,
            baseline_composite=baseline_result.composite_score if baseline_result else 0,
            baseline_quality=baseline_result.avg_quality if baseline_result else 0,
            baseline_faithfulness=baseline_result.avg_faithfulness if baseline_result else 0,
            baseline_concept=baseline_result.avg_concept_coverage if baseline_result else 0,
            baseline_pass=baseline_result.pass_rate if baseline_result else 0,
            best_composite=best_result.composite_score if best_result else 0,
            best_quality=best_result.avg_quality if best_result else 0,
            best_faithfulness=best_result.avg_faithfulness if best_result else 0,
            best_concept=best_result.avg_concept_coverage if best_result else 0,
            best_pass=best_result.pass_rate if best_result else 0,
            best_variant_name=best_variant.name if best_variant else "",
            best_changes=best_variant.changed_dimensions if best_variant else [],
            variants_tested=len(opt_run.steps),
        )
        summaries.append(summary)

    lines: List[str] = []
    lines.append(f"# Autoresearch Optimization Report\n")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"Total signals: {sum(s.signal_count for s in summaries)}  ")
    lines.append(f"Candidates evaluated: {len(summaries)}\n")
    lines.append("---\n")

    if summaries:
        summaries.sort(
            key=lambda s: s.best_composite - s.baseline_composite,
            reverse=True,
        )
        lines.append("## At a Glance\n")
        lines.append("| Candidate | Baseline | Best | Delta | Improved |")
        lines.append("|-----------|----------|------|-------|----------|")
        for s in summaries:
            delta = s.best_composite - s.baseline_composite
            improved = "Yes" if s.improved else "No"
            lines.append(
                f"| {s.base_variant} | {s.baseline_composite:.4f} | "
                f"{s.best_composite:.4f} | {delta:+.4f} | {improved} |"
            )
        lines.append("")

        for s in summaries:
            lines.append(render_run_markdown(s))

    report = "\n".join(lines)
    if output_path:
        with open(output_path, "w") as f:
            f.write(report)
        print(f"Report written to {output_path}")
    return report


# Need to import OptimizationRun and Signals for the direct API
from .optimizer import OptimizationRun  # noqa: E402
from .notes_reader import Signals  # noqa: E402
