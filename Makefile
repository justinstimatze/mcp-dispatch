.PHONY: test test-ci lint typecheck security quality

PY_SOURCES := server.py dispatch_fs.py git_transport.py git_bridge.py tests/ \
	hooks/dispatch-peek.py hooks/dispatch-gitsync-arm.py \
	bin/dispatch-status bin/dispatch-tail bin/dispatch-gitsync

test:
	uv run pytest -q

# Run tests the way CI does: with NO global/system git identity. The git
# transport configures its own repo-local identity, but a regression that leans
# on an ambient `git config user.*` would pass locally (you have one) and only
# break on a bare CI runner. This target reproduces that environment locally.
test-ci:
	HOME=$$(mktemp -d) GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null uv run pytest -q

lint:
	uv run ruff check $(PY_SOURCES)
	uv run ruff format --check $(PY_SOURCES)

typecheck:
	uv run mypy --scripts-are-modules server.py hooks/dispatch-peek.py bin/dispatch-status bin/dispatch-tail

security:
	uv run bandit -q -c pyproject.toml -r server.py hooks/dispatch-peek.py bin/dispatch-status bin/dispatch-tail

# Full gate: lint + types + security + tests (CI-faithful, identity-stripped).
quality: lint typecheck security test-ci
