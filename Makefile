
all: clean test dists

# Every tool is invoked through `uv run` so it resolves from the project
# environment. A bare `mypy`/`pytest` only works if the venv happens to be
# active, and `make lint` failed outright with "mypy: No such file or
# directory" otherwise.

.PHONY: docs
docs:
	uv run pdoc ./lazyset -o site/

.PHONY: test
test:
	uv run pytest

.PHONY: lint
lint:
	uv run ruff check lazyset test
	uv run mypy --strict lazyset

.PHONY: format
format:
	uv run ruff format lazyset test

.PHONY: format-check
format-check:
	uv run ruff format --check lazyset test

dists:
	uv run python -m build

release: dists
	uv run twine upload dist/*

.PHONY: clean
clean:
	rm -rf dist build .eggs
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -f {} +
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +
