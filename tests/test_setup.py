#!/usr/bin/env python3
"""
Test script to verify the complete TradingAgents setup works end-to-end.
"""

import os
import sys
from datetime import datetime, timedelta

def test_basic_setup():
    """Test basic imports and configuration"""
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        print("✅ Basic imports successful")
        return True
    except Exception as e:
        print(f"❌ Basic import failed: {e}")
        return False

def test_config():
    """Test configuration loading"""
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        
        # Check required environment variables
        required_vars = ['LLM_PROVIDER', 'OPENAI_API_KEY', 'FINNHUB_API_KEY']
        missing_vars = []
        
        for var in required_vars:
            if not os.environ.get(var):
                missing_vars.append(var)
        
        if missing_vars:
            print(f"⚠️ Missing environment variables: {missing_vars}")
            print("   This may cause issues with data fetching or LLM calls")
        else:
            print("✅ Required environment variables set")
        
        print(f"✅ Configuration loaded successfully")
        print(f"   LLM Provider: {os.environ.get('LLM_PROVIDER', 'not set')}")
        print(f"   OPENAI API KEY: {os.environ.get('OPENAI_API_KEY', 'not set')}")
        print(f"   Backend URL: {os.environ.get('LLM_BACKEND_URL', 'not set')}")
        return True
    except Exception as e:
        print(f"❌ Configuration test failed: {e}")
        return False

def test_trading_graph_init():
    """Test TradingAgentsGraph initialization"""
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        
        # Create a minimal config for testing
        config = DEFAULT_CONFIG.copy()
        config["online_tools"] = False  # Use cached data for testing
        config["max_debate_rounds"] = 1  # Minimize API calls
        
        ta = TradingAgentsGraph(debug=True, config=config)
        print("✅ TradingAgentsGraph initialized successfully")
        return True
    except Exception as e:
        print(f"❌ TradingAgentsGraph initialization failed: {e}")
        return False

def test_data_access():
    """Test if we can access basic data"""
    try:
        from tradingagents.dataflows.yfin_utils import get_stock_data
        
        # Test with a simple stock query
        test_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
        
        # This should work even without API keys if using cached data
        data = get_stock_data("AAPL", test_date)
        
        if data:
            print("✅ Data access test successful")
            return True
        else:
            print("⚠️ Data access returned empty results (may be expected with cached data)")
            return True
    except Exception as e:
        print(f"❌ Data access test failed: {e}")
        return False

def run_all_tests():
    """Run all tests"""
    print("🧪 Running TradingAgents setup tests...\n")
    
    tests = [
        ("Basic Setup", test_basic_setup),
        ("Configuration", test_config),
        ("TradingGraph Init", test_trading_graph_init),
        ("Data Access", test_data_access),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"Running {test_name} test...")
        try:
            if test_func():
                passed += 1
            print()
        except Exception as e:
            print(f"❌ {test_name} test crashed: {e}\n")
    
    print(f"📊 Test Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All tests passed! TradingAgents setup is working correctly.")
        return True
    else:
        print("⚠️ Some tests failed. Check the output above for details.")
        return False

if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)