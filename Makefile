# everything is phony
.PHONY: help fix check test docs clean

# 2 tabs before comments, 1 tab if there are dependencies
# help target docs stuff for us

UV = uv
RUN = $(UV) run

help:		## Show this help.
	@grep '^[^#[:space:]\.].*:' Makefile

check:		## Run linters and the type checker in check mode.
	$(RUN) ruff format --check .
	$(RUN) ruff check .
	$(RUN) ty check

fix:		## Run linters.
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

docs:		## Generate documentation.
	$(RUN) pdoc ./lazyset -o site/

# The oldest and newest supported Python, each in its own venv so the default
# .venv is never rebuilt. CI does not call this: its matrix already pins one
# interpreter per job, and runs `uv run pytest` directly.
test: check	## Run tests on the oldest and newest supported Python.
	for v in 3.11 3.14; do UV_PROJECT_ENVIRONMENT=.venv-$$v $(RUN) --python $$v pytest || exit 1; done

clean:		## Clean up build artifacts.
	rm -rf dist
