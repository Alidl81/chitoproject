.PHONY: install migrate run lint test
install:
	python -m pip install -e '.[dev]'

migrate:
	python -m app.cli migrate

run:
	uvicorn app.main:app --host 127.0.0.1 --port 8000

lint:
	ruff check app tests

test:
	pytest -q
