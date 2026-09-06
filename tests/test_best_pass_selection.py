"""Offline tests for best-pass selection in run_length_controlled_stage.

Run with: python3 tests/test_best_pass_selection.py
No pytest required. Uses a scripted fake client (no LLM calls).
"""

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import candidate_spec
from core.openrouter_client import GenerationResult, UsageRecord
from core.run_candidate import run_length_controlled_stage, visible_word_count


def summary_with_words(n: int) -> str:
    return "word " * n


def make_spec(max_passes: int, tolerance_pct: float = 0.05):
    base = candidate_spec.PROFILE_CANDIDATES["30m_deepseek-v4-flash_notthinking"]
    return dataclasses.replace(
        base,
        length_control=dataclasses.replace(base.length_control, max_passes=max_passes, tolerance_pct=tolerance_pct),
    )


class ScriptedClient:
    """Fake OpenRouterClient: returns scripted GenerationResults in order.

    When scripts run out, raise CrashError (simulated process/interruption death)
    so tests can checkpoint mid-loop exactly as a crashed run would."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls = []

    def chat_completion(self, request_body):
        self.calls.append(request_body)
        if not self.scripts:
            raise CrashError("simulated interruption: no more scripted passes")
        return self.scripts.pop(0)


class CrashError(RuntimeError):
    pass


def run_stage(*, client, spec, target_words, resume_state=None, checkpoint_callback=None):
    return run_length_controlled_stage(
        candidate_module=candidate_spec,
        spec=spec,
        stage_kind="chapter",
        stage_config=spec.chapter_stage,
        system_prompt="",
        initial_user_prompt="",
        target_words=target_words,
        mock_source_md="unused",
        client=client,
        resume_state=resume_state,
        checkpoint_callback=checkpoint_callback,
    )


def result(n_words, cost=0.0):
    return GenerationResult(
        summary_md=summary_with_words(n_words),
        estimated_visible_words=n_words,
        raw_content="",
        usage=UsageRecord(generation_cost=cost, uncached_generation_cost=cost),
        raw_response={"index": n_words},
    )


def make_checkpoint_capture():
    """Returns (capture, callback); capture["last"] = latest stage_state dict."""
    capture = {}

    def cb(state):
        capture["last"] = dict(state)

    return capture, cb


# ---- Tests -----------------------------------------------------------


def test_max_passes_earlier_closer_pass_wins():
    """Budget exhausted: pass 1 (800w, dist 200) closer than final (1500w, dist 500).
    Also covers tie: pass 2 (1200w, dist 200) ties pass 1 -> earliest wins."""
    spec = make_spec(max_passes=5)
    capture, cb = make_checkpoint_capture()
    client = ScriptedClient(result(800, 0.1), result(1200, 0.2), result(700, 0.3), result(1300, 0.4), result(1500, 0.5))
    stage = run_stage(client=client, spec=spec, target_words=1000, checkpoint_callback=cb)

    assert visible_word_count(stage.summary_md) == 800, f"got {visible_word_count(stage.summary_md)} words"
    assert stage.passes_used == 5
    assert stage.generation_cost == 1.5
    assert stage.uncached_generation_cost == 1.5
    assert len(stage.raw_responses) == 5
    assert stage.first_pass_summary_md == summary_with_words(800).strip()
    # checkpoint carries the best pass
    assert visible_word_count(capture["last"]["best_summary_md"]) == 800
    print("test_max_passes_earlier_closer_pass_wins: OK")


def test_in_range_break_returns_in_range_pass():
    """Pass 2 lands in range [950,1050] -> returned, loop stops before more passes."""
    spec = make_spec(max_passes=5)
    client = ScriptedClient(result(600), result(980, 0.7))
    stage = run_stage(client=client, spec=spec, target_words=1000)

    assert visible_word_count(stage.summary_md) == 980
    assert stage.passes_used == 2
    assert len(client.calls) == 2, "no pass generated after in-range break"
    assert stage.first_pass_summary_md == summary_with_words(600).strip()
    print("test_in_range_break_returns_in_range_pass: OK")


def test_in_range_beats_preceding_out_of_range():
    """Out-of-range pass 1 (800w, dist 200) precedes in-range pass 2 (980w, dist 20):
    in-range pass must be selected."""
    spec = make_spec(max_passes=3)
    client = ScriptedClient(result(800), result(980))
    stage = run_stage(client=client, spec=spec, target_words=1000)
    assert visible_word_count(stage.summary_md) == 980
    print("test_in_range_beats_preceding_out_of_range: OK")


def test_exact_hit_wins():
    """Pass 2 hits target exactly (dist 0) -> selected."""
    spec = make_spec(max_passes=5)
    client = ScriptedClient(result(800), result(1000))
    stage = run_stage(client=client, spec=spec, target_words=1000)
    assert visible_word_count(stage.summary_md) == 1000
    print("test_exact_hit_wins: OK")


def test_resume_preserves_best_pass():
    """Checkpoint after pass 1 (best, 880w dist 120). Resume: single worse pass
    (1400w dist 400), budget (2) exhausted -> best from before interruption kept."""
    spec = make_spec(max_passes=2)
    capture, cb = make_checkpoint_capture()
    client = ScriptedClient(result(880, 0.1), result(1350, 0.2))
    stage1 = run_stage(client=client, spec=spec, target_words=1000, checkpoint_callback=cb)
    assert visible_word_count(stage1.summary_md) == 880

    state = capture["last"]
    client2 = ScriptedClient()  # budget already exhausted at checkpoint -> no new pass
    stage2 = run_stage(client=client2, spec=spec, target_words=1000, resume_state=state)
    assert stage2.passes_used == 2
    assert visible_word_count(stage2.summary_md) == 880, "best pass lost across resume"
    assert client2.calls == [], "no repair pass expected after budget exhausted at checkpoint"
    print("test_resume_preserves_best_pass: OK")


def test_resume_old_format_checkpoint():
    """Old-format checkpoint (no best_summary_md): seed best from restored summary_md."""
    spec = make_spec(max_passes=2)
    old_state = {
        "summary_md": summary_with_words(880),
        "first_pass_summary_md": summary_with_words(880),
        "passes_used": 1,
        "generation_cost": 0.1,
        "uncached_generation_cost": 0.1,
        "raw_responses": [{"old": True}],
    }
    client = ScriptedClient(result(1400, 0.2))
    stage = run_stage(client=client, spec=spec, target_words=1000, resume_state=old_state)
    assert stage.passes_used == 2
    assert visible_word_count(stage.summary_md) == 880
    print("test_resume_old_format_checkpoint: OK")


def test_resume_checkpoint_keeps_updating_best():
    """Crash after pass 2 -> resume: pass 3 worse, pass 4 ties best distance (100);
    earliest (900) stays best across resume and checkpoint keeps it."""
    spec = make_spec(max_passes=4)
    capture, cb = make_checkpoint_capture()
    client = ScriptedClient(result(900, 0.1), result(1200, 0.2))  # passes 1-2, then simulated crash
    try:
        run_stage(client=client, spec=spec, target_words=1000, checkpoint_callback=cb)
        raise AssertionError("expected simulated interruption")
    except CrashError:
        pass
    state = capture["last"]
    assert state["passes_used"] == 2

    capture2, cb2 = make_checkpoint_capture()
    client2 = ScriptedClient(result(1500, 0.3), result(1100, 0.4))
    stage2 = run_stage(client=client2, spec=spec, target_words=1000, resume_state=state, checkpoint_callback=cb2)
    assert stage2.passes_used == 4
    assert visible_word_count(stage2.summary_md) == 900
    assert visible_word_count(capture2["last"]["best_summary_md"]) == 900
    print("test_resume_checkpoint_keeps_updating_best: OK")


def test_single_pass_in_range_unchanged():
    """Pass 1 in range: single pass, selection identical to old behavior."""
    spec = make_spec(max_passes=5)
    client = ScriptedClient(result(985))
    stage = run_stage(client=client, spec=spec, target_words=1000)
    assert visible_word_count(stage.summary_md) == 985
    assert stage.passes_used == 1
    assert len(client.calls) == 1
    print("test_single_pass_in_range_unchanged: OK")


def test_distance_tie_keeps_earliest():
    """Out-of-range tie: 800w (dist 200) and 1200w (dist 200), budget 2 -> 800w."""
    spec = make_spec(max_passes=2)
    client = ScriptedClient(result(800), result(1200))
    stage = run_stage(client=client, spec=spec, target_words=1000)
    assert visible_word_count(stage.summary_md) == 800
    print("test_distance_tie_keeps_earliest: OK")


def main():
    tests = [
        test_max_passes_earlier_closer_pass_wins,
        test_in_range_break_returns_in_range_pass,
        test_in_range_beats_preceding_out_of_range,
        test_exact_hit_wins,
        test_resume_preserves_best_pass,
        test_resume_old_format_checkpoint,
        test_resume_checkpoint_keeps_updating_best,
        test_single_pass_in_range_unchanged,
        test_distance_tie_keeps_earliest,
    ]
    for t in tests:
        t()
    print(f"\nALL {len(tests)} TESTS PASSED")


if __name__ == "__main__":
    main()