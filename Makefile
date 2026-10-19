.PHONY: help bootstrap venv index run test deploy

help:
	@echo "make bootstrap  - create venv, install deps, create .env"
	@echo "make index      - build data/vod.db + data/index.bin"
	@echo "make run        - run the bot (panel :8095, proxy :8091)"
	@echo "make test       - run the test suite"
	@echo "make deploy     - install and start the systemd unit"

bootstrap:
	./scripts/bootstrap.sh

venv:
	python3.12 -m venv .venv
	.venv/bin/pip install -U pip wheel
	.venv/bin/pip install -r requirements.txt

index:
	.venv/bin/python -m src.indexer

run:
	.venv/bin/python -m src.bot

test:
	.venv/bin/python -m pytest -q

deploy:
	sudo cp deploy/vod-delivery.service /etc/systemd/system/
	sudo systemctl daemon-reload
	sudo systemctl enable --now vod-delivery.service
