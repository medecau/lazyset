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

test: check	## Run tests.
	$(RUN) tox -p auto

clean:		## Clean up build artifacts.
	rm -rf dist
