#!/bin/bash

# Start Modified OpenWebUI with Token Usage Tracking and Reasoning Effort
# This script expects the migrated Postgres database URL in DATABASE_URL.
# It also builds the frontend if needed

set -e

echo "🚀 Starting Modified OpenWebUI with Token Usage Tracking"
echo "============================================================"

# Determine the correct base directory (works on both Mac and Ubuntu)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Build the frontend when it is absent or any real frontend input is newer than
# the generated shell. Comparing only the `src` directory mtime misses edits to
# files nested below it, which can restart the backend while continuing to serve
# an old JavaScript bundle.
FRONTEND_BUILD_NEEDED=false
if [ ! -f "build/index.html" ]; then
    FRONTEND_BUILD_NEEDED=true
elif find src static -type f -newer "build/index.html" -print -quit | grep -q .; then
    FRONTEND_BUILD_NEEDED=true
else
    for FRONTEND_INPUT in \
        package.json \
        package-lock.json \
        svelte.config.js \
        vite.config.ts \
        tailwind.config.js \
        postcss.config.js; do
        if [ -f "$FRONTEND_INPUT" ] && [ "$FRONTEND_INPUT" -nt "build/index.html" ]; then
            FRONTEND_BUILD_NEEDED=true
            break
        fi
    done
fi

if [ "$FRONTEND_BUILD_NEEDED" = "true" ]; then
    echo "📦 Building frontend (first time or source files changed)..."

    # Check if node_modules exists, if not run npm install
    if [ ! -d "node_modules" ]; then
        echo "📥 Installing npm dependencies..."
        npm install --legacy-peer-deps
    fi

    # Build the frontend
    echo "🔨 Building frontend with Vite..."
    npm run build

    if [ $? -ne 0 ]; then
        echo "❌ Frontend build failed! Please check the errors above."
        exit 1
    fi

    echo "✅ Frontend build completed successfully!"
else
    echo "✅ Frontend build is up to date (skipping build)"
fi

# API debug logging prints every streamed chunk and is very expensive for
# high-throughput reasoning models. Opt in explicitly if needed:
#   ENABLE_API_DEBUG_LOGGING=true ./start_modified.sh
export ENABLE_API_DEBUG_LOGGING="${ENABLE_API_DEBUG_LOGGING:-false}"
export ENABLE_AUTOMATIONS="${ENABLE_AUTOMATIONS:-true}"
export VECTOR_DB="${VECTOR_DB:-pgvector}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://tennisbowling:tennispass@192.168.10.2:5432/openllm}"
export DATABASE_POOL_SIZE="${DATABASE_POOL_SIZE:-20}"
export DATABASE_POOL_MAX_OVERFLOW="${DATABASE_POOL_MAX_OVERFLOW:-20}"

# Instance display name — replaces "Open WebUI" everywhere a user/crawler sees it
# (tab title, UI, share-link embeds, PWA, etc.). Override by exporting WEBUI_NAME.
export WEBUI_NAME="${WEBUI_NAME:-OpenLLM}"

if [ -z "${DATABASE_URL:-}" ]; then
    echo "❌ DATABASE_URL is required for the Postgres-only runtime."
    echo "   Example: postgresql+asyncpg://openwebui:password@host:5432/openwebui"
    exit 1
fi

# Set up environment to use existing OpenWebUI data files/uploads (Ubuntu-specific paths)
if [ -d "/home/tennisbowling" ]; then
    export DATA_DIR="/home/tennisbowling/.local/lib/python3.12/site-packages/open_webui/data"
    export PYTHONPATH="/home/tennisbowling/open-webui/backend"
    cd /home/tennisbowling/open-webui/backend
else
    # Development mode (Mac)
    export PYTHONPATH="$SCRIPT_DIR/backend"
    cd "$SCRIPT_DIR/backend"
fi

# Use system Python by default. This host keeps the Open WebUI data directory
# under the user site-packages path, and the Postgres runtime deps are installed
# into the system/user Python environment.
if [ "${USE_VENV:-false}" = "true" ] && [ -f "../venv/bin/activate" ]; then
    source ../venv/bin/activate
else
    echo "✅ Using system Python"
fi

# Start the server
PYTHON_BIN="${PYTHON_BIN:-python3}"
echo ""
echo "============================================================"
echo "📊 Starting server with enhanced features..."
echo "🔗 Web Interface will be available at: http://localhost:8081/"
echo ""
echo "✨ New Features:"
echo "   • Token Usage Display: Shows live IN/OUT/TOTAL stats above chat input"
echo "   • Reasoning Effort: Dropdown with Low/Medium/High options"
echo "   • Token Groups API: Create groups via /api/usage/groups endpoints"
echo "   • Title Generation Control: Admin can override and set model"
echo "   • Fixed Real-Time Streaming: Events now display in real-time"
echo ""
echo "Press Ctrl+C to stop the server"
echo "============================================================"
echo ""

# Trust X-Forwarded-* from the local reverse proxy (Caddy) so uvicorn sees the
# real client scheme/IP (correct https redirects, WS upgrades, rate limits) when
# fronted for HTTP/2+3. Safe here because the proxy is co-located on loopback.
# Added conditionally so an existing override in FORWARDED_ALLOW_IPS is respected.
FORWARDED_ALLOW_IPS_FLAG="--forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-*}"

exec "$PYTHON_BIN" -m uvicorn open_webui.main:app --host 0.0.0.0 --port 8081 --timeout-graceful-shutdown 75 --loop uvloop --http httptools "$FORWARDED_ALLOW_IPS_FLAG"
