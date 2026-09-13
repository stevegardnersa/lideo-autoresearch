# Specification: Automated Book Summarisation & LLM Evaluation Pipeline

## 1. Overview & Objective
This specification outlines a data pipeline designed to ingest non-fiction books, split them into chapters, and generate timed summaries (30-minute and 60-minute versions) using various Large Language Models (LLMs) via the OpenRouter API.
The system will dynamically calculate target word counts per chapter to maintain the exact proportional weight of the original book. It will evaluate different LLMs based on three primary pillars: Accuracy/Faithfulness, Speed (Latency), and Cost.
------------------------------
## 2. Core Architecture & Mathematical Framework## 2.1 Reading Time & Reduction Metrics
Rather than requesting arbitrary lengths, the pipeline calculates a strict Reduction Factor ($R$) for each book based on a baseline human reading speed of 200 words per minute (WPM).

   1. Total Book Word Count ($W_{total}$): Sum of all words across all chapters.
   2. Total Book Reading Time ($T_{total}$):
   $$T_{total} = \frac{W_{total}}{200}$$ 
   3. Target Output Word Count ($W_{target}$):
   * For a 30-minute summary: $W_{target30} = 30 \times 200 = 6,000 \text{ words}$
      * For a 60-minute summary: $W_{target60} = 60 \times 200 = 12,000 \text{ words}$
   4. Reduction Factor ($R$):
   $$R = \frac{W_{target}}{W_{total}}$$ 

## 2.2 Per-Chapter Dynamic Targets
For each chapter ($i$), the script must calculate its specific target constraints before calling the API. This ensures the model receives highly granular boundaries on the first pass, eliminating costly and slow prompt iteration loops.

* Chapter Target Words ($W_{chapter\_target}$):
$$W_{chapter\_target} = W_{chapter\_original} \times R$$ 
* Chapter Target Sentences ($S_{target}$): Calculated assuming a standard non-fiction sentence length of 18 words.
$$S_{target} = \text{round}\left(\frac{W_{chapter\_target}}{18}\right)$$ 
* API Max Token Ceiling ($T_{max}$): Calculated using a 1.35 word-to-token multiplier plus a 15% safety buffer so sentences do not truncate mid-word.
$$T_{max} = \text{round}\left((W_{chapter\_target} \times 1.35) \times 1.15\right)$$ 

------------------------------
## 3. OpenRouter API & Parameter Configuration
To ensure maximum accuracy and deterministic outputs, configurations must bypass volatile hyperparameters like length_penalty (which is often poorly supported or ignored under Top-P sampling on OpenRouter). Instead, length control is enforced structurally and via hard token ceilings.
## 3.1 Global API Hyperparameters

{
  "temperature": 0.1,
  "top_p": 0.9,
  "max_tokens": "T_max" 
}

(Note: A low temperature of 0.1 minimizes hallucinations, keeping the summary tightly aligned with the source text).
## 3.2 Model Evaluation Matrix
The pipeline must support testing across both Frontier and Flash model classes via OpenRouter:

* Frontier Models (Accuracy Focus): anthropic/claude-3.5-sonnet, openai/gpt-4o
* Flash Models (Cost/Speed Focus): google/gemini-flash-1.5, deepseek/deepseek-chat

------------------------------
## 4. Execution Prompt Template
The pipeline will inject the calculated metrics dynamically into the system/user prompt for every chapter execution pass.

You are an expert editor creating a timed summary of a non-fiction book chapter. 

CRITICAL CONSTRAINT: 
Your summary must be approximately [W_chapter_target] words long. To achieve this length perfectly, you must write EXACTLY [S_target] sentences total. Do not exceed this sentence count under any circumstance.

Source Chapter Text:
[CHAPTER_TEXT]

Provide a dense, high-information summary that strictly adheres to the sentence count constraint. Do not include conversational filler, meta-commentary, or introductory phrases (e.g., do not write "In this chapter..."). Start directly with the summary text.

------------------------------
## 5. Evaluation & Logging Framework
For every model pass, the system must log metrics into a structured format (CSV or database) to determine the optimal model choice.
## 5.1 Logged Metrics

   1. Model Name: OpenRouter model identifier string.
   2. Latency: Time elapsed from request to complete response (in seconds).
   3. Cost: Calculated using OpenRouter's exact token pricing for input/output.
   4. Length Deviation ($D$): Measures how closely the model adhered to instructions.
   $$D = \frac{\vert{}W_{actual} - W_{chapter\_target}\vert{}}{W_{chapter\_target}}$$ 

## 5.2 Accuracy Benchmarking (Algorithmic Framework)
To automate accuracy scoring without human bias, integrate an LLM-as-a-judge framework (e.g., using DeepEval or Ragas libraries) tracking two key thresholds:

* Faithfulness / Alignment Score (Target: > 0.88): Verifies the summary only contains facts present in the source chapter, heavily penalising external data or hallucinations.
* Context Recall / Coverage Score (Target: > 0.80): Ensures core arguments, themes, or data points from the source chapter were not entirely ignored.

------------------------------
## 6. Implementation Steps for the Coding Agent

   1. Ingestion Module: Parse the input text (PDF, EPUB, or TXT) and break it down by chapter into an array of strings.
   2. Math Engine: Read total words, compute $R$, and generate a dictionary of targets ($W_{chapter\_target}$, $S_{target}$, $T_{max}$) for every chapter index.
   3. Async API Orchestrator: Implement parallelized OpenRouter API calls using the max_tokens limit dynamically mapping to each chapter's $T_{max}$.
   4. Data Parser & Evaluator: Count the output tokens/words, run the accuracy scoring algorithms, calculate deviation, and write logs to the final test matrix output.

To finalize this spec, please let me know:

* What file format (e.g., TXT, Markdown, EPUB, PDF) will the raw books be in when passed to the system?
* Would you like the coding agent to build a Python-based CLI script or a Node.js/TypeScript pipeline?


