.PHONY: test lint typecheck security quality

PY_SOURCES := server.py tests/ hooks/dispatch-peek.py

test:
	uv run pytest -q

lint:
	uv run ruff check $(PY_SOURCES)
	uv run ruff format --check $(PY_SOURCES)

typecheck:
	uv run mypy server.py hooks/dispatch-peek.py

security:
	uv run bandit -q -c pyproject.toml -r server.py hooks/dispatch-peek.py

# Full gate: lint + types + security + tests.
quality: lint typecheck security test
