FROM python:3-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System updates and minimal build/runtime deps (certs, locales, etc.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (for better layer caching)
COPY requirements.txt ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt

# Copy the rest of the source code (entire repo context)
COPY . /app

# Expose the MCP server port
EXPOSE 3002

# By default, the service targets a CyberChef API at http://localhost:3000/.
# You can override from docker run with: -e CYBERCHEF_API_URL=<url>
# Note: mcp_cyberchef_service.py reads CYBERCHEF_API_URL from its module constant; if you
# wish to make it configurable via env var, set it before execution (example below).

# Start the MCP service
# Pass runtime parameters via docker run, e.g.:
#   docker run --rm -p 3002:3002 \
#     cyberchef-mcp \
#     --api-url http://host.docker.internal:3000/ \
#     --host 0.0.0.0 \
#     --port 3002
ENTRYPOINT ["python", "-u", "-m", "mcp_cyberchef_service"]
