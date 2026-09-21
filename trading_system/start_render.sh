#!/bin/bash
# Render.com startup script for Paper Trading System

echo "========================================================================"
echo "🚀 PAPER TRADING SYSTEM - STARTING ON RENDER.COM"
echo "========================================================================"
echo ""
echo "Environment: Production (Cloud)"
echo "Strategy: Long Put"
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

# Create paper_trades.json if it doesn't exist
if [ ! -f paper_trades.json ]; then
    echo "📄 Creating paper_trades.json..."
    echo '{"trades": [], "capital": 100000, "strategy": "LONG_PUT"}' > paper_trades.json
    echo "✅ paper_trades.json created"
fi
echo ""

echo "========================================================================"
echo "🎯 STARTING PAPER TRADING..."
echo "========================================================================"
echo ""

# Start paper trading with error handling.
#
# v2 replaces the old script: it consumes the real signal engine
# (analysis.signal_engine.generate) instead of entering unconditionally, prices
# trades from real chain quotes instead of a synthetic formula, and reconciles
# any position left open by a crashed previous run before generating a new
# signal. See scripts/paper_trading.py's module docstring for why it was
# retired rather than continuing to be the deployed entrypoint.
python scripts/paper_trading_v2.py --underlying NIFTY --hold-days 7 --interval 60

# If paper trading exits, log the reason
EXIT_CODE=$?
echo ""
echo "========================================================================"
echo "⚠️ PAPER TRADING STOPPED"
echo "Exit Code: $EXIT_CODE"
echo "Time: $(date)"
echo "========================================================================"

# Exit with the same code
exit $EXIT_CODE
