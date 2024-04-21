# Makefile
PYTHON ?= ./venv/bin/python
APP ?= tidalidarr
TESTS ?= tests
REPOSITORY_URL ?= ghcr.io/dorskfr/$(APP)
IMAGE_TAG ?= latest

setup:
	python -m venv venv
	$(PYTHON) -m pip install --upgrade pip setuptools wheel
	$(PYTHON) -m pip install -Ur requirements-dev.txt

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} \;
	find . -type d -name .cache -prune -exec rm -rf {} \;
	find . -type d -name .mypy_cache -prune -exec rm -rf {} \;
	find . -type d -name .pytest_cache -prune -exec rm -rf {} \;
	find . -type d -name .ruff_cache -prune -exec rm -rf {} \;
	find . -type d -name venv -prune -exec rm -rf {} \;

lint:
	$(PYTHON) -m ruff check ./$(APP) $(TESTS)
	$(PYTHON) -m ruff format --check ./$(APP) $(TESTS)
	$(PYTHON) -m mypy --cache-dir .cache/mypy_cache ./$(APP) $(TESTS)
	$(PYTHON) -m vulture --min-confidence=100 ./$(APP) $(TESTS)

lint/fix:
	$(PYTHON) -m ruff check --fix-only ./$(APP) $(TESTS)
	$(PYTHON) -m ruff format ./$(APP) $(TESTS)

run:
	$(PYTHON) -m $(APP)

test:
	$(PYTHON) -m pytest --rootdir=. -o cache_dir=.cache/pytest_cache $(TESTS) -s -x -v $(options)

docker/build:
	docker build \
		--platform linux/amd64 \
		--build-arg PROJECT_NAME=$(APP) \
		--build-arg VERSION=$(IMAGE_TAG) \
		--build-arg PYTHON_VERSION=$(shell cat .python-version | awk -F. '{print $$1"."$$2}') \
		-t $(REPOSITORY_URL):$(IMAGE_TAG) \
		.

docker/push:
	docker push  $(REPOSITORY_URL):$(IMAGE_TAG)

docker/run:
	docker run --rm -it --name $(APP) $(REPOSITORY_URL):$(IMAGE_TAG)

.PHONY: $(shell grep -E '^([a-zA-Z_-]|\/)+:' $(MAKEFILE_LIST) | awk -F':' '{print $2}' | sed 's/:.*//')
