UV ?= uv

.PHONY: setup check dev smoke dry-run dev-telemetry-worker dev-demand-worker

setup:
	$(UV) sync --frozen
	npm ci

check:
	$(UV) run --frozen ruff check .
	$(UV) run --frozen ruff format --check .
	$(UV) run --frozen pytest

dev:
	$(UV) run --frozen --env-file .env python scripts/dev.py

smoke:
	$(UV) run --frozen --env-file .env python scripts/smoke.py

dry-run:
	$(UV) run --frozen pywrangler deploy --dry-run -c wrangler.jsonc --outdir dist/telemetry
	$(UV) run --frozen pywrangler deploy --dry-run -c wrangler.demand.jsonc --outdir dist/demand

dev-telemetry-worker:
	$(UV) run --frozen pywrangler dev -c wrangler.jsonc --port 8787

dev-demand-worker:
	$(UV) run --frozen pywrangler dev -c wrangler.demand.jsonc --port 8788
