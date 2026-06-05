.PHONY: test lint typecheck security quality

test:
	uv run pytest -q

lint:
	uv run ruff check server.py tests/
	uv run ruff format --check server.py tests/

typecheck:
	uv run mypy server.py

security:
	uv run bandit -q -c pyproject.toml -r server.py

# Full gate: lint + types + security + tests.
quality: lint typecheck security test
