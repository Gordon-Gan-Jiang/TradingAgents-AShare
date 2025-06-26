# TradingAgents Test Suite

This directory contains all test scripts for validating the TradingAgents setup and configuration.

## Test Scripts

### 🧪 `run_tests.py` - Automated Test Runner
**Purpose**: Automatically detects your LLM provider and runs appropriate tests.

**Usage**:
```bash
# Run all tests (auto-detects provider from LLM_PROVIDER env var)
# Always run from project root, not from tests/ directory
python tests/run_tests.py

# In Docker
docker compose --profile openai run --rm app-openai python tests/run_tests.py
docker compose --profile ollama exec app-ollama python tests/run_tests.py
```

**Important**: Always run the test runner from the **project root directory**, not from inside the `tests/` directory. The runner automatically handles path resolution and changes to the correct working directory.

**Features**:
- Auto-detects LLM provider from environment
- Runs provider-specific tests only
- Provides comprehensive test summary
- Handles timeouts and error reporting

---

### 🔌 `test_openai_connection.py` - OpenAI API Tests
**Purpose**: Validates OpenAI API connectivity and functionality.

**Tests**:
- ✅ API key validation
- ✅ Chat completion (using `gpt-4o-mini`)
- ✅ Embeddings (using `text-embedding-3-small`)
- ✅ Configuration validation

**Usage**:
```bash
# From project root
python tests/test_openai_connection.py

# In Docker
docker compose --profile openai run --rm app-openai python tests/test_openai_connection.py
```

**Requirements**:
- `OPENAI_API_KEY` environment variable
- `LLM_PROVIDER=openai`

---

### 🦙 `test_ollama_connection.py` - Ollama Connectivity Tests
**Purpose**: Validates Ollama server connectivity and model availability.

**Tests**:
- ✅ Ollama API accessibility
- ✅ Model availability (`qwen3:0.6b`, `nomic-embed-text`)
- ✅ OpenAI-compatible API functionality
- ✅ Chat completion and embeddings

**Usage**:
```bash
# From project root
python tests/test_ollama_connection.py

# In Docker
docker compose --profile ollama exec app-ollama python tests/test_ollama_connection.py
```

**Requirements**:
- Ollama server running
- Required models downloaded
- `LLM_PROVIDER=ollama`

---

### ⚙️ `test_setup.py` - General Setup Validation
**Purpose**: Validates basic TradingAgents setup and configuration.

**Tests**:
- ✅ Python package imports
- ✅ Configuration loading
- ✅ TradingAgentsGraph initialization
- ✅ Data access capabilities

**Usage**:
```bash
# From project root
python tests/test_setup.py

# In Docker
docker compose --profile openai run --rm app-openai python tests/test_setup.py
docker compose --profile ollama exec app-ollama python tests/test_setup.py
```

**Requirements**:
- TradingAgents dependencies installed
- Basic environment configuration

---

## Test Results Interpretation

### ✅ Success Indicators
- All tests pass
- API connections established
- Models available and responding
- Configuration properly loaded

### ❌ Common Issues

**OpenAI Tests Failing**:
- Check `OPENAI_API_KEY` is set correctly
- Verify API key has sufficient quota
- Ensure internet connectivity

**Ollama Tests Failing**:
- Verify Ollama service is running
- Check if models are downloaded (`./init-ollama.sh`)
- Confirm `ollama list` shows required models

**Setup Tests Failing**:
- Check Python dependencies are installed
- Verify environment variables are set
- Ensure `.env` file is properly configured

---

## Quick Testing Commands

**⚠️ Important**: Always run these commands from the **project root directory** (not from inside `tests/`):

```bash
# Test everything automatically (from project root)
python tests/run_tests.py

# Test specific provider (from project root)
LLM_PROVIDER=openai python tests/run_tests.py
LLM_PROVIDER=ollama python tests/run_tests.py

# Test individual components (from project root)
python tests/test_openai_connection.py
python tests/test_ollama_connection.py
python tests/test_setup.py
```

**Why from project root?**
- Tests need to import the `tradingagents` package
- The `tradingagents` package is located in the project root
- Running from `tests/` directory would cause import errors

---

## Adding New Tests

To add new tests:

1. Create new test script in `tests/` directory
2. Follow the naming convention: `test_<component>.py`
3. Include proper error handling and status reporting
4. Update `run_tests.py` if automatic detection is needed
5. Document the test in this README

**Test Script Template**:
```python
#!/usr/bin/env python3
"""Test script for <component>"""

def test_component():
    """Test <component> functionality."""
    try:
        # Test implementation
        print("✅ Test passed")
        return True
    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False

if __name__ == "__main__":
    success = test_component()
    exit(0 if success else 1)
```