
.PHONY: up up-lite down logs lint format seed reset lock lock-check

PYTEST_ARGS ?=
BACKEND_PYTHON ?= $(shell cat backend/.python-version)
# 锁文件生成/校验用的 uv 与 docker/api.Dockerfile 构建镜像的 uv 保持同一版本（单一来源：Dockerfile）。
# 必须用 astral 带 Python 的镜像变体：裸 ghcr.io/astral-sh/uv 是 distroless（无 sh/libc），uv lock 跑不起来。
UV_VERSION ?= $(shell grep -oE 'ghcr.io/astral-sh/uv:[0-9.]+' docker/api.Dockerfile | head -1 | cut -d: -f2)
UV_IMAGE ?= ghcr.io/astral-sh/uv:$(UV_VERSION)-python$(BACKEND_PYTHON)-trixie-slim
# pwd -W 让 Git Bash 输出 Windows 盘符路径；MSYS_NO_PATHCONV 阻止 /ws 被改写成 Git 安装目录
UV_DOCKER = MSYS_NO_PATHCONV=1 docker run --rm -v "$$(pwd -W 2>/dev/null || pwd)/backend:/ws" -w /ws $(UV_IMAGE)

up:
	@if [ ! -f .env ]; then \
		echo "Error: .env file not found. Please create it from .env.template"; \
		exit 1; \
	fi
	docker compose up -d

down:
	docker compose down

reset:
	@if [ ! -f .env ]; then \
		echo "Error: .env file not found. Please create it from .env.template"; \
		exit 1; \
	fi
	docker compose down
	rm -rf docker/volumes
	docker compose up -d
	@echo "Waiting for api to be ready..."
	@until docker compose exec -T api true >/dev/null 2>&1; do sleep 2; done
	$(MAKE) seed

up-lite:
	@if [ ! -f .env ]; then \
		echo "Error: .env file not found. Please create it from .env.template"; \
		exit 1; \
	fi
	LITE_MODE=true VITE_USE_RUNS_API=false docker compose up -d postgres redis minio api web

logs:
	@docker logs --tail=50 api-dev
	@echo "\n\nBranch: $$(git branch --show-current)"
	@echo "Commit ID: $$(git rev-parse HEAD)"
	@echo "System: $$(uname -a)"

seed:
	docker compose exec api uv run python scripts/seed_initial_users.py

######################
# DEPENDENCY LOCK
######################

# 改了 backend/pyproject.toml 或 backend/package/pyproject.toml 后必须执行，并把 uv.lock 一起提交。
# 镜像构建用 uv sync --locked，锁文件漂移会直接构建失败（宿主机无需安装 uv）。
lock:
	$(UV_DOCKER) uv lock

# 校验 uv.lock 与 pyproject 一致，与 CI backend-lock-check 完全相同的命令
lock-check:
	$(UV_DOCKER) uv lock --check

######################
# LINTING AND FORMATTING
######################

format:
	cd backend && UV_PYTHON=$(BACKEND_PYTHON) uv run ruff format package
	cd backend && UV_PYTHON=$(BACKEND_PYTHON) uv run ruff check package --fix
	cd backend && UV_PYTHON=$(BACKEND_PYTHON) uv run ruff check --select I package --fix
	cd web && pnpm run format
	cd web && pnpm run lint
