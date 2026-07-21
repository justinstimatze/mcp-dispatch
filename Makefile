.PHONY: install test test-ci lint typecheck security quality

PY_SOURCES := server.py dispatch_fs.py git_transport.py git_bridge.py notify_policy.py \
	dispatch_common.py gitsync_service.py install.py tests/ \
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

# gitsync_service.py and bin/dispatch-gitsync are in BOTH lists deliberately: they
# interpolate config-supplied paths and --env values into a systemd unit and shell
# out to systemctl, which makes them the most scan-worthy code in the repo. CI runs
# only `ruff check .` and pytest, so these targets are the only place bandit and
# mypy ever see anything — a module missing here is a module nobody checks.
TYPED := server.py dispatch_common.py gitsync_service.py \
	hooks/dispatch-peek.py hooks/dispatch-arm.py hooks/dispatch-gitsync-arm.py \
	bin/dispatch-status bin/dispatch-tail bin/dispatch-wait bin/dispatch-gitsync

typecheck:
	uv run mypy --scripts-are-modules $(TYPED)

security:
	uv run bandit -q -c pyproject.toml -r $(TYPED)

# Full gate: lint + types + security + tests (CI-faithful, identity-stripped).
quality: lint typecheck security test-ci
