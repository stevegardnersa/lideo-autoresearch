PYTHON ?= python
BOOKS_ROOT ?= data/books
ARTIFACTS_ROOT ?= artifacts
BENCH_DIR ?= bench

rubrics:
	$(PYTHON) tools/build_rubrics.py --books-root $(BOOKS_ROOT) --artifacts-root $(ARTIFACTS_ROOT)

bench:
	$(PYTHON) tools/build_bench.py --books-root $(BOOKS_ROOT) --bench-dir $(BENCH_DIR) --dev-books 10 --gate-books 4 --holdout-books 4 --seed 42

smoke:
	$(PYTHON) tools/build_rubrics.py --books-root $(BOOKS_ROOT) --artifacts-root $(ARTIFACTS_ROOT)
	$(PYTHON) tools/build_bench.py --books-root $(BOOKS_ROOT) --bench-dir $(BENCH_DIR) --dev-books 1 --gate-books 1 --holdout-books 1 --seed 42
	$(PYTHON) core/run_candidate.py --bench chapter_fast --profile 30m --mock --write-results
	$(PYTHON) core/run_candidate.py --bench book_gate --profile 30m --mock --write-results

leaderboard:
	$(PYTHON) tools/leaderboard.py

cf-smoke:
	$(PYTHON) -m pip show functions-framework >/dev/null 2>&1 || $(PYTHON) -m pip install functions-framework
	PORT=$$($(PYTHON) -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"); \
	LLM_API_KEY=sk-placeholder $(PYTHON) -m functions_framework --target summarize --signature-type http --source cloud_function/main.py --port $$PORT & \
	FF_PID=$$!; \
	trap 'kill $$FF_PID 2>/dev/null || true' EXIT INT TERM; \
	sleep 4; \
	$(PYTHON) cloud_function/cf_smoke.py http://localhost:$$PORT

corpus-report:
	$(PYTHON) tools/corpus_report.py --books-root $(BOOKS_ROOT)
