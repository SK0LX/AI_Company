# AI Office — one container, the whole platform (admin + dashboard + Telegram bots
# + agents). Run it on any VPS for 24/7; "restart = update" (rebuild & up -d).
FROM python:3.13-slim

# git: needed by the maintainer's self-modify worktrees + repo-read tool.
# node + the Claude Code CLI: the optional bash-Claude engine (src/claude_bridge.py)
# shells out to `claude -p`. Auth is done once at runtime via `claude login`
# (credentials persist in the /root/.claude volume — see docker-compose).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App source (see .dockerignore for what's excluded).
COPY . .

# Container defaults: bind to all interfaces, keep state under /app (volumes).
ENV PYTHONUNBUFFERED=1 \
    ADMIN_HOST=0.0.0.0 \
    ADMIN_PORT=8100 \
    WORKSPACE_DIR=/app/workspace \
    WIKI_DIR=/app/wiki \
    DB_PATH=/app/data/memory.sqlite \
    SELF_REPO_DIR=/app

EXPOSE 8100
VOLUME ["/app/data", "/app/workspace", "/app/wiki"]

CMD ["python", "main.py"]
