.PHONY: install test test-ci lint typecheck security quality

PY_SOURCES := server.py dispatch_fs.py git_transport.py git_bridge.py notify_policy.py \
	dispatch_common.py install.py tests/ \
	hooks/dispatch-peek.py hooks/dispatch-arm.py hooks/dispatch-gitsync-arm.py \
	bin/dispatch-status bin/dispatch-tail bin/dispatch-wait bin/dispatch-gitsync \
	scripts/

# One-command setup: sync deps, register the MCP server, wire the hooks.
# Idempotent — safe to re-run. `make install ARGS=--dry-run` to preview.
install:
	python3 install.py $(ARGS)

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
	uv run mypy --scripts-are-modules server.py dispatch_common.py hooks/dispatch-peek.py hooks/dispatch-arm.py hooks/dispatch-gitsync-arm.py bin/dispatch-status bin/dispatch-tail bin/dispatch-wait

security:
	uv run bandit -q -c pyproject.toml -r server.py dispatch_common.py hooks/dispatch-peek.py hooks/dispatch-arm.py hooks/dispatch-gitsync-arm.py bin/dispatch-status bin/dispatch-tail bin/dispatch-wait

# Full gate: lint + types + security + tests (CI-faithful, identity-stripped).
quality: lint typecheck security test-ci
