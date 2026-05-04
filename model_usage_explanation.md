# chapter_model vs composer_model

**Session ID:** ses_2238c2ac7ffeR2RX4EgF2sMqEi
**Created:** 4/30/2026, 3:34:31 PM
**Updated:** 4/30/2026, 3:35:24 PM

---

## User

what is the difference between chapter_model and composer_model

---

## Assistant (Plan · MiniMax M2.7 · 28.3s)

_Thinking:_

The user is asking about the difference between `chapter_model` and `composer_model`. This seems to be a question about their codebase. Let me search for these terms to understand what they refer to.

**Tool: grep**

**Input:**
```json
{
  "pattern": "chapter_model|composer_model"
}
```

**Output:**
```
Found 609 matches (showing first 100)
/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/results.tsv:
  Line 1: timestamp	run_id	benchmark_version	corpus_version	rubric_version	scoring_version	judge_version	profile	bench	candidate_name	candidate_sha256	hypothesis	chapter_model	composer_model	judge_model	mean_quality	mean_utility	mean_faithfulness	mean_concept_coverage	mean_final_length_error_pct	mean_first_pass_length_error_pct	mean_passes_used	mean_uncached_generation_cost	mean_generation_cost	hard_fail_rate	worst_genre_macro	worst_genre_macro_utility	worst_genre_macro_quality	genre_macro_spread_utility	n_genre_macros	run_artifact	catalog_snapshot	price_snapshot	notes


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/core/run_candidate.py:
  Line 1419:         "profile\tbench\tcandidate_name\tcandidate_sha256\thypothesis\tchapter_model\tcomposer_model\tjudge_model\t"

  Line 1441:         str(run_manifest.get("chapter_model", "")),

  Line 1442:         str(run_manifest.get("composer_model", "")),

  Line 1648:             "chapter_model": spec.chapter_stage.model,

  Line 1649:             "composer_model": spec.composer_stage.model,


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t032654z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t032654z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t031729z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t022427z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.state.json:
  Line 21:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 22:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t022427z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.json:
  Line 18:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 19:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t013510z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.state.json:
  Line 21:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 22:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t013510z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.json:
  Line 18:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 19:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t010242z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.state.json:
  Line 21:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 22:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t010242z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.json:
  Line 18:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 19:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t004645z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t004645z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t002703z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t002703z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t001542z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t000813z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260429t235626z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260429t235626z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260429t234415z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260429t230706z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260429t230706z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260429t225822z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260429t225822z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t223607z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t223607z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t222447z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t220958z__booksum-v3__chapter_small__60m__baseline_60m_dense_faithful_v1.state.json:
  Line 21:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 22:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t220958z__booksum-v3__chapter_small__60m__baseline_60m_dense_faithful_v1.json:
  Line 18:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 19:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t220900z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t220900z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t212109z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t212109z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t200756z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t200614z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t200614z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 18:     "chapter_model": "minimax/minimax-m2.7",

  Line 19:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v3/20260429t190241z__booksum-v3__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t093217z__booksum-v2__chapter_small__30m_minimax_nott__30m_minimax_notthinking_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t092100z__booksum-v2__chapter_small__30m_minimax_nott__30m_minimax_notthinking_v1.state.json:
  Line 21:     "chapter_model": "minimax/minimax-m2.7",

  Line 22:     "composer_model": "openai/gpt-5-mini",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t083852z__booksum-v2__chapter_small__60m__baseline_60m_dense_faithful_v1.state.json:
  Line 21:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 22:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t083852z__booksum-v2__chapter_small__60m__baseline_60m_dense_faithful_v1.json:
  Line 18:     "chapter_model": "deepseek/deepseek-v3.2",

  Line 19:     "composer_model": "deepseek/deepseek-v3.2",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t083032z__booksum-v2__chapter_small__60m_mimo25pro_th__60m_mimo25pro_thinking_v1.state.json:
  Line 21:     "chapter_model": "xiaomi/mimo-v2.5-pro",

  Line 22:     "composer_model": "xiaomi/mimo-v2.5-pro",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t083032z__booksum-v2__chapter_small__60m_mimo25pro_th__60m_mimo25pro_thinking_v1.json:
  Line 18:     "chapter_model": "xiaomi/mimo-v2.5-pro",

  Line 19:     "composer_model": "xiaomi/mimo-v2.5-pro",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t082434z__booksum-v2__chapter_small__60m_mimo25pro_th__60m_mimo25pro_thinking_v1.state.json:
  Line 21:     "chapter_model": "xiaomi/mimo-v2.5-pro",

  Line 22:     "composer_model": "xiaomi/mimo-v2.5-pro",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t082434z__booksum-v2__chapter_small__60m_mimo25pro_th__60m_mimo25pro_thinking_v1.json:
  Line 18:     "chapter_model": "xiaomi/mimo-v2.5-pro",

  Line 19:     "composer_model": "xiaomi/mimo-v2.5-pro",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t060851z__booksum-v2__chapter_small__60m_dv4flash_thi__60m_dv4flash_thinking_v1.state.json:
  Line 21:     "chapter_model": "deepseek/deepseek-v4-flash",

  Line 22:     "composer_model": "deepseek/deepseek-v4-flash",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t060851z__booksum-v2__chapter_small__60m_dv4flash_thi__60m_dv4flash_thinking_v1.json:
  Line 18:     "chapter_model": "deepseek/deepseek-v4-flash",

  Line 19:     "composer_model": "deepseek/deepseek-v4-flash",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t055916z__booksum-v2__chapter_small__60m_dv4pro_think__60m_dv4pro_thinking_v1.state.json:
  Line 21:     "chapter_model": "deepseek/deepseek-v4-pro",

  Line 22:     "composer_model": "deepseek/deepseek-v4-pro",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v2/20260429t055916z__booksum-v2__chapter_small__60m_dv4pro_think__60m_dv4pro_thinking_v1.json:
  Line 18:     "chapter_model": "deepseek/deepseek-v4-pro",

  Line 19:     "composer_model": "deepseek/deepseek-v4-pro",


(Results truncated: showing 100 of 609 matches (509 hidden). Consider using a more specific path or pattern.)
```

---

## Assistant (Plan · MiniMax M2.7 · 6.8s)

_Thinking:_

The grep results show that `chapter_model` and `composer_model` are configuration fields in run manifests. They appear to be two different LLM models used at different stages of a pipeline. Let me look at the source code to understand what these stages do.

Let me look at the source code to understand the pipeline stages:


**Tool: grep**

**Input:**
```json
{
  "pattern": "chapter_stage|composer_stage"
}
```

**Output:**
```
Found 779 matches (showing first 100)
/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/core/run_candidate.py:
  Line 108:     chapter_extra = spec.chapter_stage.extra_body or {}

  Line 596:     format_instructions = getattr(candidate_module, "FORMAT_INSTRUCTIONS", {})[spec.composer_stage.format_mode]

  Line 926:             stage_config=spec.chapter_stage,

  Line 1113:             stage_config=spec.chapter_stage,

  Line 1152:     composer_run_payload = progress.get("composer_stage_run") if isinstance(progress.get("composer_stage_run"), Mapping) else None

  Line 1195:             stage_config=spec.composer_stage,

  Line 1218:         progress["composer_stage_run"] = serialize_stage_run(composer_run)

  Line 1273:     progress["composer_stage_run"] = serialize_stage_run(composer_run)

  Line 1648:             "chapter_model": spec.chapter_stage.model,

  Line 1649:             "composer_model": spec.composer_stage.model,

  Line 1655:                     "order": list(spec.chapter_stage.provider_order),

  Line 1656:                     "allow_fallbacks": bool(spec.chapter_stage.allow_fallbacks),

  Line 1659:                     "order": list(spec.composer_stage.provider_order),

  Line 1660:                     "allow_fallbacks": bool(spec.composer_stage.allow_fallbacks),


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t032654z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 38:       "chapter_stage_sha256": "dfde7f9e29041689390ec3bfde7112ee3ce674cc15c15c181818f9ac3583e655",

  Line 39:       "composer_stage_sha256": "b08d5b1d3e9aeb8325d1c338912e1fb43450a2462b1d0df8f3e8d0bdc978d041",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t032654z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.json:
  Line 35:       "chapter_stage_sha256": "dfde7f9e29041689390ec3bfde7112ee3ce674cc15c15c181818f9ac3583e655",

  Line 36:       "composer_stage_sha256": "b08d5b1d3e9aeb8325d1c338912e1fb43450a2462b1d0df8f3e8d0bdc978d041",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t031729z__booksum-v4__chapter_small__30m__baseline_30m_dense_recall_then_compose_v1.state.json:
  Line 36:       "chapter_stage_sha256": "dfde7f9e29041689390ec3bfde7112ee3ce674cc15c15c181818f9ac3583e655",

  Line 37:       "composer_stage_sha256": "b08d5b1d3e9aeb8325d1c338912e1fb43450a2462b1d0df8f3e8d0bdc978d041",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t022427z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.state.json:
  Line 36:       "chapter_stage_sha256": "5a6a9b87e888c851b47e2cef365d8f70c262bad6befb0b2dff2ec04e246a231e",

  Line 37:       "composer_stage_sha256": "23d48480998b7be9e8dcc6e6287f333f63636f92e3896a92ba5d2f28a115b2e0",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t022427z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.json:
  Line 33:       "chapter_stage_sha256": "5a6a9b87e888c851b47e2cef365d8f70c262bad6befb0b2dff2ec04e246a231e",

  Line 34:       "composer_stage_sha256": "23d48480998b7be9e8dcc6e6287f333f63636f92e3896a92ba5d2f28a115b2e0",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t013510z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.state.json:
  Line 36:       "chapter_stage_sha256": "5a6a9b87e888c851b47e2cef365d8f70c262bad6befb0b2dff2ec04e246a231e",

  Line 37:       "composer_stage_sha256": "23d48480998b7be9e8dcc6e6287f333f63636f92e3896a92ba5d2f28a115b2e0",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/runs/booksum-v4/20260430t013510z__booksum-v4__chapter_small__60m__baseline_60m_dense_faithful_v1.json:
  Line 33:       "chapter_stage_sha256": "5a6a9b87e888c851b47e2cef365d8f70c262bad6befb0b2dff2ec04e246a231e",

  Line 34:       "composer_stage_sha256": "23d48480998b7be9e8dcc6e6287f333f63636f92e3896a92ba5d2f28a115b2e0",


/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/candidate_spec.py:
  Line 81:     chapter_stage_multiplier_30m: float = 1.20

  Line 82:     chapter_stage_multiplier_60m: float = 1.00

  Line 98:     chapter_stage: StageConfig

  Line 99:     composer_stage: StageConfig

  Line 310: def chapter_stage_total_target_words(

  Line 317:         cfg.chapter_stage_multiplier_30m if profile == "30m" else cfg.chapter_stage_multiplier_60m

  Line 399:     total_stage_target = chapter_stage_total_target_words(total_book_visible_words, profile, cfg)

  Line 474:     stage = spec.chapter_stage

  Line 499:     stage = spec.chapter_stage

  Line 560:             FORMAT_INSTRUCTIONS[spec.chapter_stage.format_mode],

  Line 570:     stage = spec.composer_stage

  Line 595:     stage = spec.composer_stage

  Line 667:         chapter_stage=StageConfig(

  Line 689:         composer_stage=StageConfig(

  Line 720:             chapter_stage_multiplier_30m=1.20,

  Line 721:             chapter_stage_multiplier_60m=1.00,

  Line 738:         chapter_stage=StageConfig(

  Line 755:         composer_stage=StageConfig(

  Line 786:             chapter_stage_multiplier_30m=1.20,

  Line 787:             chapter_stage_multiplier_60m=1.00,

  Line 804:         chapter_stage=StageConfig(

  Line 826:         composer_stage=StageConfig(

  Line 860:             chapter_stage_multiplier_30m=1.20,

  Line 861:             chapter_stage_multiplier_60m=1.00,

  Line 875:         chapter_stage=StageConfig(

  Line 895:         composer_stage=StageConfig(

  Line 929:             chapter_stage_multiplier_30m=1.20,

  Line 930:             chapter_stage_multiplier_60m=1.00,

  Line 944:         chapter_stage=StageConfig(

  Line 962:         composer_stage=StageConfig(

  Line 994:             chapter_stage_multiplier_30m=1.20,

  Line 995:             chapter_stage_multiplier_60m=1.00,

  Line 1009:         chapter_stage=StageConfig(

  Line 1027:         composer_stage=StageConfig(

  Line 1059:             chapter_stage_multiplier_30m=1.20,

  Line 1060:             chapter_stage_multiplier_60m=1.00,

  Line 1074:         chapter_stage=StageConfig(

  Line 1092:         composer_stage=StageConfig(

  Line 1124:             chapter_stage_multiplier_30m=1.20,

  Line 1125:             chapter_stage_multiplier_60m=1.00,

  Line 1139:         chapter_stage=StageConfig(

  Line 1157:         composer_stage=StageConfig(

  Line 1189:             chapter_stage_multiplier_30m=1.20,

  Line 1190:             chapter_stage_multiplier_60m=1.00,

  Line 1204:         chapter_stage=StageConfig(

  Line 1222:         composer_stage=StageConfig(

  Line 1254:             chapter_stage_multiplier_30m=1.20,

  Line 1255:             chapter_stage_multiplier_60m=1.00,

  Line 1269:         chapter_stage=StageConfig(

  Line 1287:         composer_stage=StageConfig(

  Line 1319:             chapter_stage_multiplier_30m=1.20,

  Line 1320:             chapter_stage_multiplier_60m=1.00,

  Line 1334:         chapter_stage=StageConfig(

  Line 1352:         composer_stage=StageConfig(

  Line 1384:             chapter_stage_multiplier_30m=1.20,

  Line 1385:             chapter_stage_multiplier_60m=1.00,

  Line 1399:         chapter_stage=StageConfig(

  Line 1417:         composer_stage=StageConfig(

  Line 1449:             chapter_stage_multiplier_30m=1.20,

  Line 1450:             chapter_stage_multiplier_60m=1.00,

  Line 1464:         chapter_stage=StageConfig(

  Line 1482:         composer_stage=StageConfig(

  Line 1514:             chapter_stage_multiplier_30m=1.20,

  Line 1515:             chapter_stage_multiplier_60m=1.00,

  Line 1529:         chapter_stage=StageConfig(

  Line 1547:         composer_stage=StageConfig(

  Line 1579:             chapter_stage_multiplier_30m=1.20,

  Line 1580:             chapter_stage_multiplier_60m=1.00,

  Line 1594:         chapter_stage=StageConfig(

  Line 1612:         composer_stage=StageConfig(

  Line 1644:             chapter_stage_multiplier_30m=1.20,

  Line 1645:             chapter_stage_multiplier_60m=1.00,


(Results truncated: showing 100 of 779 matches (679 hidden). Consider using a more specific path or pattern.)
```

---

## Assistant (Plan · MiniMax M2.7 · 4.2s)

_Thinking:_

This is a booksummary generation pipeline. Let me read the key source files to understand the difference between chapter_stage and composer_stage.

**Tool: read**

**Input:**
```json
{
  "filePath": "/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/candidate_spec.py",
  "offset": 660,
  "limit": 120
}
```

**Output:**
```
<path>/Users/stevegardner/Documents/Projects/Lideo/autoresearch/tool/candidate_spec.py</path>
<type>file</type>
<content>
660:     return request
661: 
662: 
663: PROFILE_CANDIDATES: Dict[Profile, CandidateSpec] = {
664:     "30m": CandidateSpec(
665:         name="baseline_30m_dense_recall_then_compose_v1",
666:         profile="30m",
667:         chapter_stage=StageConfig(
668:             model="minimax/minimax-m2.7",
669:             temperature=0.2,
670:             seed=42,
671:             max_tokens=8192,
672:             format_mode="markdown_sections",
673:             context_mode="chapter_plus_toc_and_meta",
674:             prompt_components={
675:                 "system_style": "dense_faithful",
676:                 "detail_policy": "mechanisms_first",
677:                 "qualifier_policy": "strict",
678:                 "structure_policy": "heading_aware",
679:                 "example_policy": "explanatory_only",
680:                 "terminology_policy": "keep_source_terms",
681:                 "anti_fluff_policy": "hard",
682:             },
683:             extra_body={
684:                 "thinking": {"type": "disabled"},
685:                 "order": ["minimax"],
686:                 "allow_fallbacks": False,
687:             },
688:         ),
689:         composer_stage=StageConfig(
690:             model="openai/gpt-5-mini",
691:             temperature=0.2,
692:             seed=42,
693:             max_tokens=8192,
694:             format_mode="markdown_sections",
695:             context_mode="chapter_plus_toc_and_meta",
696:             prompt_components={
697:                 "system_style": "architectural_synthesizer",
698:                 "synthesis_policy": "thesis_then_frameworks",
699:                 "detail_policy": "balanced_dense",
700:                 "qualifier_policy": "strict",
701:                 "structure_policy": "theme_clustered",
702:                 "terminology_policy": "keep_source_terms",
703:                 "anti_fluff_policy": "hard",
704:             },
705:         ),
706:         composer_mode="summaries_only",
707:         length_control=LengthControlConfig(
708:             max_passes=5,
709:             tolerance_pct=0.08,
710:             hard_tolerance_pct=0.15,
711:             repair_strategy="edit_existing",
712:             repair_more_prompt_id="expand_mechanisms_first",
713:             repair_less_prompt_id="shrink_dedup_first",
714:         ),
715:         budget_allocator=BudgetAllocatorConfig(
716:             words_per_minute=200,
717:             allocation_alpha=0.90,
718:             min_chapter_share=0.03,
719:             max_chapter_share=0.18,
720:             chapter_stage_multiplier_30m=1.20,
721:             chapter_stage_multiplier_60m=1.00,
722:             max_summary_to_source_ratio=0.90,
723:         ),
724:         use_json_schema=False,
725:         json_schema_name="summary_response_30m",
726:         notes=(
727:             "30m baseline keeps chapter summaries slightly over-complete so the composer can "
728:             "deduplicate across chapters."
729:         ),
730:         scoring_gates_override=ScoringGatesOverride(
731:             min_faithfulness=0.20,
732:             min_concept_coverage=0.10,
733:         ),
734:     ),
735:     "60m": CandidateSpec(
736:         name="baseline_60m_dense_faithful_v1",
737:         profile="60m",
738:         chapter_stage=StageConfig(
739:             model="deepseek/deepseek-v3.2",
740:             temperature=0.2,
741:             seed=42,
742:             max_tokens=8192,
743:             format_mode="markdown_sections",
744:             context_mode="chapter_plus_toc_and_meta",
745:             prompt_components={
746:                 "system_style": "dense_faithful",
747:                 "detail_policy": "balanced_dense",
748:                 "qualifier_policy": "strict",
749:                 "structure_policy": "heading_aware",
750:                 "example_policy": "explanatory_only",
751:                 "terminology_policy": "keep_source_terms",
752:                 "anti_fluff_policy": "hard",
753:             },
754:         ),
755:         composer_stage=StageConfig(
756:             model="deepseek/deepseek-v3.2",
757:             temperature=0.2,
758:             seed=42,
759:             max_tokens=8192,
760:             format_mode="markdown_sections",
761:             context_mode="chapter_plus_toc_and_meta",
762:             prompt_components={
763:                 "system_style": "dedupe_synthesizer",
764:                 "synthesis_policy": "progressive_argument",
765:                 "detail_policy": "balanced_dense",
766:                 "qualifier_policy": "strict",
767:                 "structure_policy": "theme_clustered",
768:                 "terminology_policy": "keep_source_terms",
769:                 "anti_fluff_policy": "hard",
770:             },
771:         ),
772:         composer_mode="summaries_only",
773:         length_control=LengthControlConfig(
774:             max_passes=5,
775:             tolerance_pct=0.08,
776:             hard_tolerance_pct=0.15,
777:             repair_strategy="edit_existing",
778:             repair_more_prompt_id="expand_mechanisms_first",
779:             repair_less_prompt_id="shrink_dedup_first",

(Showing lines 660-779 of 1987. Use offset=780 to continue.)
</content>
```

---

## Assistant (Plan · MiniMax M2.7 · 12.4s)

_Thinking:_

Now I understand the difference. This is a book summarization system with two stages:

1. **chapter_stage**: Generates individual chapter summaries. Uses a `dense_faithful` style focused on "mechanisms_first" with detail policy.

2. **composer_stage**: Takes all chapter summaries and synthesizes them into a coherent whole-book summary. Uses an "architectural_synthesizer" or "dedupe_synthesizer" style with "thesis_then_frameworks" or "progressive_argument" synthesis policies.

The `chapter_model` and `composer_model` are simply the LLM model identifiers used for each respective stage. They can be the same or different models depending on the configuration.

Let me provide a concise answer.

Based on your codebase, `chapter_model` and `composer_model` are LLM models used at different stages of a **book summarization pipeline**:

**chapter_model** — generates individual chapter summaries. Uses `dense_faithful` style, focused on capturing mechanisms and details from each chapter.

**composer_model** — takes all chapter summaries and synthesizes them into a coherent whole-book summary. Uses `architectural_synthesizer` or `dedupe_synthesizer` style to deduplicate and restructure across chapters.

They're stored in run manifests to track which models powered each stage (e.g., your runs show `chapter_model: minimax/minimax-m2.7` with `composer_model: openai/gpt-5-mini` — often different models for cost/quality tradeoffs).

---

