COMPOSE ?= docker compose
API_PORT ?= 8000
LLM_PORT ?= 8100
COMMERCE_PORT ?= 8200
export API_PORT LLM_PORT COMMERCE_PORT
BENCH    = uv run --project harness python -m harness.bench \
           --sut http://localhost:$(API_PORT) --llm http://localhost:$(LLM_PORT) --commerce http://localhost:$(COMMERCE_PORT)

.PHONY: up down restart fresh logs ps smoke bench bench-nochaos spike workload test

up:            ## build and start everything, wait until healthy
	$(COMPOSE) up -d --build --wait

down:          ## stop everything and remove volumes
	$(COMPOSE) down -v

restart: down up

fresh:         ## wipe state and start clean (every bench does this first)
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) up -d --build --wait

logs:          ## follow logs
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

smoke: fresh         ## 30 tickets, no chaos: a quick sanity check
	$(BENCH) --profile smoke --no-chaos

bench: fresh         ## the scored run: ~390 tickets with a spike, chaos on
	$(BENCH) --profile standard

bench-nochaos: fresh ## the same load without chaos
	$(BENCH) --profile standard --no-chaos

spike: fresh         ## a heavier spike
	$(BENCH) --profile spike

workload:      ## regenerate harness/data/workload.json (seed 42)
	uv run --project harness python -m harness.workload --seed 42 --out harness/data/workload.json

test:          ## harness self-tests
	uv run --project harness pytest -q harness/tests
