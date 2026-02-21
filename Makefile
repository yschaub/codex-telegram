.PHONY: check-poetry install dev test lint format clean help run run-debug smoke-codex run-remote remote-attach remote-stop

POETRY ?= poetry

# Default target
help:
	@echo "Available commands:"
	@echo "  install    - Install production dependencies"
	@echo "  dev        - Install development dependencies"
	@echo "  test       - Run tests"
	@echo "  lint       - Run linting checks"
	@echo "  format     - Format code"
	@echo "  clean      - Clean up generated files"
	@echo "  run        - Run the bot"
	@echo "  run-debug  - Run the bot with debug logs"
	@echo "  smoke-codex - Smoke test Codex new+resume path"
	@echo "  run-remote - Start bot in tmux on remote Mac (unlocks keychain)"
	@echo "  remote-attach - Attach to running bot tmux session"
	@echo "  remote-stop   - Stop the bot tmux session"

check-poetry:
	@if ! command -v $(POETRY) >/dev/null 2>&1; then \
		echo "Poetry is required but was not found in PATH."; \
		echo "Install Poetry: https://python-poetry.org/docs/#installation"; \
		exit 1; \
	fi

install: check-poetry
	$(POETRY) install --no-dev

dev: check-poetry
	$(POETRY) install
	@if [ -f .pre-commit-config.yaml ]; then \
		if $(POETRY) run pre-commit --version >/dev/null 2>&1; then \
			$(POETRY) run pre-commit install || echo "pre-commit hooks were not installed"; \
		else \
			echo "Skipping pre-commit hook install (pre-commit is not installed)."; \
		fi; \
	else \
		echo "Skipping pre-commit hook install (.pre-commit-config.yaml not found)."; \
	fi

test: check-poetry
	$(POETRY) run pytest

lint: check-poetry
	$(POETRY) run black --check src tests
	$(POETRY) run isort --check-only src tests
	$(POETRY) run flake8 src tests
	$(POETRY) run mypy src

format: check-poetry
	$(POETRY) run black src tests
	$(POETRY) run isort src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/

run: check-poetry
	$(POETRY) run python -m src.main

# For debugging
run-debug: check-poetry
	$(POETRY) run python -m src.main --debug

smoke-codex: check-poetry
	$(POETRY) run python scripts/codex_smoke.py

# Remote Mac Mini (SSH session)
run-remote: check-poetry  ## Start bot on remote Mac in tmux (persists after SSH disconnect)
	security unlock-keychain ~/Library/Keychains/login.keychain-db
	tmux new-session -d -s codex-bot '$(POETRY) run python -m src.main'
	@echo "Bot started in tmux session 'codex-bot'"
	@echo "  Attach: make remote-attach"
	@echo "  Stop:   make remote-stop"

remote-attach:  ## Attach to running bot tmux session
	tmux attach -t codex-bot

remote-stop:  ## Stop the bot tmux session
	tmux kill-session -t codex-bot
