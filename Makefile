.PHONY: up down logs load restart

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f agent-api

load:
	docker compose --profile load run --build --rm load-generator

restart:
	docker compose down && docker compose up --build -d
