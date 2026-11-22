# Container image for ECS Fargate (§2.6.1).
#
# Must ship the MCP server too, not just the agent: mcp_server/server.py is
# launched as a stdio SUBPROCESS on every turn (agent/harness.py), so it has to
# exist inside the image and be runnable by the same interpreter. A slim image
# that drops it would pass a health check and fail on the first real ticket.

FROM python:3.12-slim

# PYTHONUNBUFFERED so [GATE]/[RETRIEVER] lines reach CloudWatch immediately
# rather than sitting in a buffer until the process exits — in a long-running
# server that means never.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONWARNINGS=ignore::UserWarning

WORKDIR /app

# Dependencies first, as their own layer: requirements.txt changes far less
# often than the source, so image rebuilds on a code change skip the pip install.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/       ./agent/
COPY mcp_server/  ./mcp_server/
COPY rag/         ./rag/
COPY data/        ./data/
COPY eval/        ./eval/
COPY app.py main.py scenarios.py ./

# Non-root: the task has no reason to run privileged, and this is the cheapest
# hardening available.
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8000

# The ALB health check hits /health. Docker's own HEALTHCHECK is included so
# `docker run` locally behaves the same way as the ECS task definition.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
