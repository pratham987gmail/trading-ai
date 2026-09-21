"""
Simple Angel One Connection Test - No Complex Dependencies

Just tests if you can connect and get basic data.
"""

import sys
from pathlib import Path
import os
from datetime import datetime

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_angel_connection():
    """Simple test of Angel One connection."""
    
    print("\n" + "="*70)
    print("🔌 ANGEL ONE API - SIMPLE CONNECTION TEST")
    print("="*70 + "\n")
    
    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv()
    
    # Get credentials
    api_key = os.getenv('ANGEL_API_KEY')
    client_id = os.getenv('ANGEL_CLIENT_ID')
    password = os.getenv('ANGEL_PASSWORD')
    totp_secret = os.getenv('ANGEL_TOTP_SECRET')
    
    print("📋 Credentials Check:")
    print(f"   API Key: {'✅ Found' if api_key else '❌ Missing'}")
    print(f"   Client ID: {'✅ Found' if client_id else '❌ Missing'}")
    print(f"   Password: {'✅ Found' if password else '❌ Missing'}")
    print(f"   TOTP Secret: {'✅ Found' if totp_secret else '❌ Missing'}")
    
    if not all([api_key, client_id, password, totp_secret]):
        print("\n❌ Missing credentials in .env file!")
        return False
    
    print()
    
    # Try to import and use SmartAPI
    try:
        from SmartApi import SmartConnect
        import pyotp
        
        print("🔑 Authenticating with Angel One...")
        
        # Generate TOTP
        totp = pyotp.TOTP(totp_secret).now()
        
        # Create session
        smartApi = SmartConnect(api_key=api_key)
        
        # Login
        data = smartApi.generateSession(client_id, password, totp)
        
        if data['status']:
            print("   ✅ Authentication successful!")
            print(f"   Client: {client_id}")
            print(f"   Session created at: {datetime.now().strftime('%H:%M:%S')}")
            
            # Try to get LTP
            print("\n📊 Testing Market Data...")
            try:
                # Get NIFTY 50 token (99926000)
                ltp_data = smartApi.ltpData("NSE", "NIFTY 50", "99926000")
                
                if ltp_data and ltp_data.get('data'):
                    ltp = ltp_data['data'].get('ltp')
                    print(f"   ✅ NIFTY 50 LTP: ₹{ltp}")
                else:
                    print("   ⚠️  No LTP data received")
                    
            except Exception as e:
                print(f"   ⚠️  Market data: {e}")
            
            # Try to get profile
            print("\n💰 Testing Account Info...")
            try:
                profile = smartApi.getProfile(data['data']['refreshToken'])
                if profile and profile.get('data'):
                    name = profile['data'].get('name', 'N/A')
                    print(f"   ✅ Account: {name}")
                else:
                    print("   ⚠️  No profile data")
            except Exception as e:
                print(f"   ⚠️  Profile: {e}")
            
            print("\n" + "="*70)
            print("✅ CONNECTION TEST PASSED!")
            print("="*70)
            print()
            print("🎉 Your Angel One API is working!")
            print()
            print("Next steps:")
            print("  1. You can now collect live market data")
            print("  2. The system can fetch option chains")
            print("  3. Ready for live signal generation")
            print()
            
            return True
            
        else:
            print(f"   ❌ Authentication failed: {data.get('message')}")
            return False
            
    except ImportError as e:
        print(f"❌ Missing library: {e}")
        print("\nInstall with: pip install smartapi-python pyotp")
        return False
        
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("\nPossible issues:")
        print("  1. Wrong API key or client ID")
        print("  2. Wrong password")
        print("  3. Wrong TOTP secret")
        print("  4. Network/firewall blocking connection")
        return False


if __name__ == "__main__":
    test_angel_connection()
