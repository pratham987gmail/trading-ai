#!/bin/bash
# Render.com startup script for Data Collection Service

echo "========================================================================"
echo "📊 DATA COLLECTION SERVICE - STARTING ON RENDER.COM"
echo "========================================================================"
echo ""
echo "Environment: Production (Cloud)"
echo "Service: Angel One Data Collection"
echo "Region: $(echo $RENDER_REGION)"
echo "Service: $(echo $RENDER_SERVICE_NAME)"
echo ""
echo "========================================================================"
echo ""

# Change to trading_system directory
cd /opt/render/project/src/trading_system

# Verify environment variables
echo "🔍 Checking environment variables..."
if [ -z "$ANGEL_API_KEY" ]; then
    echo "❌ ERROR: ANGEL_API_KEY not set!"
    exit 1
fi
if [ -z "$ANGEL_CLIENT_ID" ]; then
    echo "❌ ERROR: ANGEL_CLIENT_ID not set!"
    exit 1
fi
if [ -z "$ANGEL_PASSWORD" ]; then
    echo "❌ ERROR: ANGEL_PASSWORD not set!"
    exit 1
fi
if [ -z "$ANGEL_TOTP_SECRET" ]; then
    echo "❌ ERROR: ANGEL_TOTP_SECRET not set!"
    exit 1
fi
echo "✅ All environment variables configured"
echo ""

# Create .env file from environment variables
echo "📝 Creating .env file..."
cat > .env << EOF
ANGEL_API_KEY=$ANGEL_API_KEY
ANGEL_CLIENT_ID=$ANGEL_CLIENT_ID
ANGEL_PASSWORD=$ANGEL_PASSWORD
ANGEL_TOTP_SECRET=$ANGEL_TOTP_SECRET
EOF
echo "✅ .env file created"
echo ""

# Create database directory if needed
echo "📁 Setting up database directory..."
mkdir -p data
echo "✅ Database directory ready"
echo ""

echo "========================================================================"
echo "🎯 STARTING DATA COLLECTION..."
echo "========================================================================"
echo ""
echo "Collection settings:"
echo "  - Symbols: NIFTY BANKNIFTY"
echo "  - Interval: 60 seconds"
echo "  - Mode: Continuous 24/7"
echo "  - Storage: SQLite database (data/options.db, ChainRecorder schema)"
echo ""

# Start data collection with error handling.
#
# Phase 3.6 Section G: this used to invoke scripts/simple_data_collector.py,
# which wrote real option quotes (call_oi=0, call_volume=0 placeholders, no
# actual per-strike fetch) into a different database (data/option_chains.db,
# the recorder/storage.py schema) and never called secure_third_party_loggers()
# before constructing its own SmartConnect session. scripts/record_chains.py
# wraps the hardened core/chain_recorder.py::ChainRecorder, which does call
# secure_third_party_loggers() (via core/angel_client.py::connect()), writes
# real per-leg quotes to data/options.db (core/option_store.py schema, the
# one Phase 3.5/3.6 research code actually reads), and checkpoints every
# cycle in the `cycles` table. See PHASE_3_6_SECTION_G_COLLECTOR_RECONCILIATION.md
# for the full comparison. simple_data_collector.py is kept in the repo (not
# deleted) in case it serves some other purpose later, but it is no longer
# the deployed collector.
python scripts/record_chains.py --symbols NIFTY BANKNIFTY --interval 60

# If data collection exits, log the reason
EXIT_CODE=$?
echo ""
echo "========================================================================"
echo "⚠️ DATA COLLECTION STOPPED"
echo "Exit Code: $EXIT_CODE"
echo "Time: $(date)"
echo "========================================================================"

# Exit with the same code
exit $EXIT_CODE
