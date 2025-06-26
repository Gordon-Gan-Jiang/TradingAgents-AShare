#!/usr/bin/env python3
"""
Test runner script for TradingAgents

This script automatically detects the LLM provider and runs appropriate tests.
"""

import os
import sys
import subprocess

def get_llm_provider():
    """Get the configured LLM provider from environment."""
    return os.environ.get("LLM_PROVIDER", "").lower()

def run_test_script(script_name):
    """Run a test script and return success status."""
    try:
        print(f"🧪 Running {script_name}...")
        result = subprocess.run([sys.executable, script_name], 
                              capture_output=True, text=True, timeout=120)
        
        if result.returncode == 0:
            print(f"✅ {script_name} passed")
            if result.stdout:
                print(f"   Output: {result.stdout.strip()}")
            return True
        else:
            print(f"❌ {script_name} failed")
            if result.stderr:
                print(f"   Error: {result.stderr.strip()}")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"⏰ {script_name} timed out")
        return False
    except Exception as e:
        print(f"💥 {script_name} crashed: {e}")
        return False

def main():
    """Main test runner function."""
    print("🚀 TradingAgents Test Runner")
    print("=" * 50)
    
    # Get project root directory (parent of tests directory)
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(tests_dir)
    os.chdir(project_root)
    
    provider = get_llm_provider()
    print(f"📋 Detected LLM Provider: {provider or 'not set'}")
    
    tests_run = []
    tests_passed = []
    
    # Always run setup tests
    if run_test_script("tests/test_setup.py"):
        tests_passed.append("tests/test_setup.py")
    tests_run.append("tests/test_setup.py")
    
    # Run provider-specific tests
    if provider == "openai":
        print("\n🔍 Running OpenAI-specific tests...")
        if run_test_script("tests/test_openai_connection.py"):
            tests_passed.append("tests/test_openai_connection.py")
        tests_run.append("tests/test_openai_connection.py")
        
    elif provider == "ollama":
        print("\n🔍 Running Ollama-specific tests...")
        if run_test_script("tests/test_ollama_connection.py"):
            tests_passed.append("tests/test_ollama_connection.py")
        tests_run.append("tests/test_ollama_connection.py")
        
    else:
        print(f"\n⚠️ Unknown or unset LLM provider: '{provider}'")
        print("   Running all connectivity tests...")
        
        for test_script in ["tests/test_openai_connection.py", "tests/test_ollama_connection.py"]:
            if run_test_script(test_script):
                tests_passed.append(test_script)
            tests_run.append(test_script)
    
    # Summary
    print("\n" + "=" * 50)
    print(f"📊 Test Results: {len(tests_passed)}/{len(tests_run)} tests passed")
    
    for test in tests_run:
        status = "✅ PASS" if test in tests_passed else "❌ FAIL"
        print(f"   {test}: {status}")
    
    if len(tests_passed) == len(tests_run):
        print("\n🎉 All tests passed! TradingAgents is ready to use.")
        return 0
    else:
        print(f"\n⚠️ {len(tests_run) - len(tests_passed)} test(s) failed. Check configuration.")
        return 1

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)