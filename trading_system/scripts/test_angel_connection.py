"""
Test Angel One API Connection

Verifies that:
1. Credentials are valid
2. Can authenticate successfully
3. Can fetch market data
4. Can get option chains
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Settings
from core.angel_client import AngelClient


def test_connection():
    """Test Angel One API connection."""
    print("\n" + "="*70)
    print("🔌 ANGEL ONE API CONNECTION TEST")
    print("="*70 + "\n")
    
    # Load settings
    settings = Settings()
    
    print("📋 Configuration:")
    print(f"   Client ID: {settings.ANGEL_CLIENT_ID}")
    print(f"   API Key: {settings.ANGEL_API_KEY[:8]}...")
    print(f"   Mode: {settings.TRADING_MODE}")
    print()
    
    # Create client
    print("🔑 Authenticating...")
    try:
        client = AngelClient(settings)
        print("   ✅ Authentication successful!")
        print(f"   Session token received")
    except Exception as e:
        print(f"   ❌ Authentication failed: {e}")
        return False
    
    print()
    
    # Test market data - Get NIFTY LTP
    print("📊 Testing Market Data...")
    try:
        ltp_data = client.get_ltp("NIFTY", "NSE")
        if ltp_data:
            print(f"   ✅ NIFTY LTP: ₹{ltp_data.get('ltp', 'N/A')}")
        else:
            print("   ⚠️  No LTP data received")
    except Exception as e:
        print(f"   ❌ Market data failed: {e}")
    
    print()
    
    # Test option chain
    print("📈 Testing Option Chain...")
    try:
        # Get current expiry
        from datetime import datetime, timedelta
        
        # Try to get option chain for NIFTY
        chain = client.get_option_chain("NIFTY")
        
        if chain and chain.get('strikes'):
            print(f"   ✅ Option chain retrieved")
            print(f"   Expiry: {chain.get('expiry')}")
            print(f"   Spot: ₹{chain.get('spot')}")
            print(f"   Strikes: {len(chain.get('strikes', []))}")
            
            # Show sample strikes
            strikes = chain.get('strikes', [])[:3]
            print(f"\n   Sample strikes:")
            for s in strikes:
                strike = s.get('strike')
                call_ltp = s.get('call', {}).get('ltp', 'N/A')
                put_ltp = s.get('put', {}).get('ltp', 'N/A')
                print(f"      {strike}: Call ₹{call_ltp} | Put ₹{put_ltp}")
        else:
            print("   ⚠️  No option chain data")
    except Exception as e:
        print(f"   ❌ Option chain failed: {e}")
    
    print()
    
    # Test account info
    print("💰 Testing Account Info...")
    try:
        profile = client.get_profile()
        if profile:
            print(f"   ✅ Account: {profile.get('name', 'N/A')}")
            print(f"   Client ID: {profile.get('clientcode', 'N/A')}")
        else:
            print("   ⚠️  No profile data")
    except Exception as e:
        print(f"   ❌ Account info failed: {e}")
    
    print()
    print("="*70)
    print("✅ CONNECTION TEST COMPLETE")
    print("="*70)
    print()
    print("💡 Next Steps:")
    print("   1. If all tests passed, you're ready to collect live data!")
    print("   2. Run: python scripts/collect_live_data.py")
    print("   3. This will start recording option chains for backtesting")
    print()
    
    return True


if __name__ == "__main__":
    test_connection()
