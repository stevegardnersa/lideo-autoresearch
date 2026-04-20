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

corpus-report:
	$(PYTHON) tools/corpus_report.py --books-root $(BOOKS_ROOT)
