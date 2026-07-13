
all: clean test dists

.PHONY: docs
docs:
	$(MAKE) -C docs html

.PHONY: test
test:
	pytest

.PHONY: lint
lint:
	ruff check lazyset test
	mypy --strict lazyset

.PHONY: format
format:
	ruff format lazyset test

.PHONY: format-check
format-check:
	ruff format --check lazyset test

dists:
	python -m build

release: dists
	pip install -q twine
	twine upload dist/*

.PHONY: clean
clean:
	rm -rf dist build .eggs
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -f {} +
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +
