# ============================================================
# Autonomous AIOps Platform — Makefile
# Usage: make <target>
# ============================================================

.PHONY: up down logs demo load-test restart clean status help

help:
	@echo ""
	@echo "  Autonomous AIOps Platform — Available Commands"
	@echo "  ─────────────────────────────────────────────────"
	@echo "  make up          Start entire platform"
	@echo "  make down        Stop everything"
	@echo "  make demo        Run MongoDB incident simulation"
	@echo "  make load-test   Generate background traffic (5 min)"
	@echo "  make logs        Show AI engine logs (live)"
	@echo "  make status      Show all container health"
	@echo "  make restart     Restart AI engine only"
	@echo "  make clean       Remove containers + volumes (DESTRUCTIVE)"
	@echo ""
	@echo "  URLs after 'make up':"
	@echo "  ─────────────────────────────────────────────────"
	@echo "  AIOps Dashboard  http://localhost:8000/incidents/ui"
	@echo "  Grafana          http://localhost:3000  (admin/admin123)"
	@echo "  Prometheus       http://localhost:9090"
	@echo ""

up:
	@echo "Starting AIOps platform..."
	@test -f .env || (echo "ERROR: .env not found. Run: cp .env.example .env" && exit 1)
	docker compose up -d --build
	@echo ""
	@echo "✅ Platform started. Waiting 15s for services to be ready..."
	@sleep 15
	@make status

down:
	docker compose down

logs:
	docker compose logs -f aiops-engine

demo:
	@echo "Running MongoDB incident simulation..."
	python demo/simulate_mongodb_incident.py

load-test:
	python demo/load_test.py --threads 5 --duration 300

restart:
	docker compose restart aiops-engine predictive-monitor

status:
	@echo ""
	@echo "Service Status:"
	@echo "───────────────────────────────────────────────────"
	@docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
	@echo ""

clean:
	@echo "WARNING: This deletes all data (Prometheus, Grafana, Loki)"
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ]
	docker compose down -v --remove-orphans
	@echo "Clean complete."

simulate-incident:
	curl -s -X POST "http://localhost:8000/incidents/simulate?alert_name=MongoDBConnectionErrors" | python3 -m json.tool

break-payment-api:
	curl -s -X POST http://localhost:8001/break | python3 -m json.tool

heal-payment-api:
	curl -s -X POST http://localhost:8001/heal | python3 -m json.tool

break-mongodb:
	curl -s -X POST http://localhost:8002/simulate-node-maintenance | python3 -m json.tool

restore-mongodb:
	curl -s -X POST http://localhost:8002/restore | python3 -m json.tool
