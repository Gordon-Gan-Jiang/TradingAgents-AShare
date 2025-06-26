#!/usr/bin/env python3
"""
Test script to verify OpenAI API connection is working.
"""

import os
import sys
from openai import OpenAI

def test_openai_connection():
    """Test if OpenAI API is accessible and responding."""
    
    # Get configuration from environment
    api_key = os.environ.get("OPENAI_API_KEY")
    backend_url = os.environ.get("LLM_BACKEND_URL", "https://api.openai.com/v1")
    provider = os.environ.get("LLM_PROVIDER", "openai")
    
    print(f"Testing OpenAI API connection:")
    print(f"  Provider: {provider}")
    print(f"  Backend URL: {backend_url}")
    print(f"  API Key: {'✅ Set' if api_key and api_key != '<your-openai-key>' else '❌ Not set or using placeholder'}")
    
    if not api_key or api_key == "<your-openai-key>":
        print("❌ OPENAI_API_KEY is not set or still using placeholder value")
        print("   Please set your OpenAI API key in the .env file")
        return False
    
    # Test 1: Initialize OpenAI client
    try:
        client = OpenAI(
            api_key=api_key,
            base_url=backend_url
        )
        print("✅ OpenAI client initialized successfully")
    except Exception as e:
        print(f"❌ Failed to initialize OpenAI client: {e}")
        return False
    
    # Test 2: Test chat completion with a simple query
    try:
        print("🧪 Testing chat completion...")
        response = client.chat.completions.create(
            model="gpt-4o-mini",  # Use the most cost-effective model for testing
            messages=[
                {"role": "user", "content": "Hello! Please respond with exactly: 'OpenAI API test successful'"}
            ],
            max_tokens=50,
            temperature=0
        )
        
        if response.choices and response.choices[0].message.content:
            content = response.choices[0].message.content.strip()
            print(f"✅ Chat completion successful")
            print(f"   Model: {response.model}")
            print(f"   Response: {content}")
            print(f"   Tokens used: {response.usage.total_tokens if response.usage else 'unknown'}")
        else:
            print("❌ Chat completion returned empty response")
            return False
            
    except Exception as e:
        print(f"❌ Chat completion test failed: {e}")
        if "insufficient_quota" in str(e).lower():
            print("   💡 This might be a quota/billing issue. Check your OpenAI account.")
        elif "invalid_api_key" in str(e).lower():
            print("   💡 Invalid API key. Please check your OPENAI_API_KEY.")
        return False
    
    # Test 3: Test embeddings (optional, for completeness)
    try:
        print("🧪 Testing embeddings...")
        response = client.embeddings.create(
            model="text-embedding-3-small",  # Cost-effective embedding model
            input="This is a test sentence for embeddings."
        )
        
        if response.data and len(response.data) > 0 and response.data[0].embedding:
            embedding = response.data[0].embedding
            print(f"✅ Embeddings successful")
            print(f"   Model: {response.model}")
            print(f"   Embedding dimension: {len(embedding)}")
            print(f"   Tokens used: {response.usage.total_tokens if response.usage else 'unknown'}")
        else:
            print("❌ Embeddings returned empty response")
            return False
            
    except Exception as e:
        print(f"❌ Embeddings test failed: {e}")
        print("   ⚠️ Embeddings test failed but chat completion worked. This is usually fine for basic usage.")
        # Don't return False here as embeddings might not be critical for all use cases
    
    return True

def test_config_validation():
    """Validate the configuration is properly set for OpenAI."""
    
    provider = os.environ.get("LLM_PROVIDER", "").lower()
    backend_url = os.environ.get("LLM_BACKEND_URL", "")
    
    print("\n🔧 Configuration validation:")
    
    if provider != "openai":
        print(f"⚠️ LLM_PROVIDER is '{provider}', expected 'openai'")
        print("   The app might still work if the provider supports OpenAI-compatible API")
    else:
        print("✅ LLM_PROVIDER correctly set to 'openai'")
    
    if "openai.com" in backend_url:
        print("✅ Using official OpenAI API endpoint")
    elif backend_url:
        print(f"ℹ️ Using custom endpoint: {backend_url}")
        print("   Make sure this endpoint is OpenAI-compatible")
    else:
        print("⚠️ LLM_BACKEND_URL not set, using default")
    
    # Check for common environment issues
    finnhub_key = os.environ.get("FINNHUB_API_KEY")
    if not finnhub_key or finnhub_key == "<your_finnhub_api_key_here>":
        print("⚠️ FINNHUB_API_KEY not set - financial data fetching may not work")
    else:
        print("✅ FINNHUB_API_KEY is set")
    
    return True

if __name__ == "__main__":
    print("🧪 OpenAI API Connection Test\n")
    
    config_ok = test_config_validation()
    api_ok = test_openai_connection()
    
    print(f"\n📊 Test Results:")
    print(f"   Configuration: {'✅ OK' if config_ok else '❌ Issues'}")
    print(f"   API Connection: {'✅ OK' if api_ok else '❌ Failed'}")
    
    if config_ok and api_ok:
        print("\n🎉 All tests passed! OpenAI API is ready for TradingAgents.")
        print("💡 You can now run the trading agents with OpenAI as the LLM provider.")
    else:
        print("\n💥 Some tests failed. Please check your configuration and API key.")
        print("💡 Make sure OPENAI_API_KEY is set correctly in your .env file.")
    
    sys.exit(0 if (config_ok and api_ok) else 1)