.PHONY: bootstrap up down logs health shell validate build

bootstrap:
	cp -n .env.example .env || true
	mkdir -p workspace
	docker compose run --rm naumi-bootstrap

build:
	docker compose build

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f naumi-api

health:
	curl -fsS http://127.0.0.1:$${NAUMI_API_PORT:-8080}/api/v1/health

shell:
	docker compose run --rm naumi-api bash

validate:
	docker compose run --rm naumi-bootstrap
