.PHONY: install test backtest optimize evolve

install:
	pip install -e ".[dev]"

test:
	python3 -m pytest -q

backtest:
	PYTHONPATH=src python3 -m polytrage.cli backtest --fixtures

optimize:
	PYTHONPATH=src python3 -m polytrage.cli optimize --fixtures

evolve:
	PYTHONPATH=src python3 scripts/evolve.py --fixtures
