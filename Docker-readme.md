# 🚀 Docker Setup for Trading Agents

This guide provides instructions for running the Trading Agents application within a secure and reproducible Docker environment. Using Docker simplifies setup, manages dependencies, and ensures a consistent experience across different machines.

The recommended method is using `docker-compose`, which handles the entire stack, including the Ollama server and model downloads.

## Prerequisites

Before you begin, ensure you have the following installed:

- [**Docker**](https://docs.docker.com/get-docker/)
- [**Docker Compose**](https://docs.docker.com/compose/install/) (usually included with Docker Desktop)

## 🤔 Which Option Should I Choose?

| Feature                   | OpenAI                    | Local Ollama                  |
| ------------------------- | ------------------------- | ----------------------------- |
| **Setup Time**            | 2-5 minutes               | 15-30 minutes                 |
| **Cost**                  | ~$0.01-0.05 per query     | Free after setup              |
| **Quality**               | GPT-4o (excellent)        | Depends on model              |
| **Privacy**               | Data sent to OpenAI       | Fully private                 |
| **Internet Required**     | Yes                       | No (after setup)              |
| **Hardware Requirements** | None                      | 4GB+ RAM recommended          |
| **Model Downloads**       | None                      | Depends on model              |
| **Best For**              | Quick testing, production | Privacy-focused, cost control |

**💡 Recommendation**: Start with OpenAI for quick testing, then switch to Ollama for production if privacy/cost is important.

## ⚡ Quickstart

### Option A: Using OpenAI (Recommended for beginners)

```bash
# 1. Clone the repository
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents

# 2. Create and configure environment file
cp .env.example .env
# Edit .env: Set LLM_PROVIDER=openai and add your OPENAI_API_KEY

# 3. Build and run with OpenAI
docker compose --profile openai build
docker compose --profile openai run -it app-openai
```

### Option B: Using Local Ollama (Free but requires more setup)

```bash
# 1. Clone the repository
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents

# 2. Create environment file
cp .env.example .env
# Edit .env: Set LLM_PROVIDER=ollama

# 3. Start Ollama service
docker compose --profile ollama up -d --build

# 4. Initialize models (first time only)
# Linux/macOS:
./init-ollama.sh
# Windows Command Prompt:
init-ollama.bat


# 5. Run the command-line app
docker compose --profile ollama run -it app-ollama
```

## 🛠️ Build Methods

Choose your preferred build method:

### Method 1: Quick Build (Recommended)

```bash
# Standard Docker build
docker build -t trading-agents .

# Or with docker-compose
docker compose build
```

### Method 2: Optimized Build (Advanced)

For faster rebuilds with caching:

**Linux/macOS:**

```bash
# Build with BuildKit optimization
./build.sh

# With testing
./build.sh --test

# Clean cache and rebuild
./build.sh --clean --test
```

**Windows Command Prompt:**

```cmd
REM Build with BuildKit optimization
build.bat

REM With testing
build.bat --test

REM Clean cache and rebuild
build.bat --clean --test
```

**Benefits of Optimized Build:**

- ⚡ 60-90% faster rebuilds via BuildKit cache
- 🔄 Automatic fallback to simple build if needed
- 📊 Cache statistics and build info
- 🧪 Built-in testing capabilities

## Step-by-Step Instructions

### Step 1: Clone the Repository

```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

### Step 2: Configure Your Environment (`.env` file)

The application is configured using an environment file. Create your own `.env` file by copying the provided template.

```bash
cp .env.example .env
```

#### Option A: OpenAI Configuration (Recommended)

Edit your `.env` file and set:

```env
# LLM Provider Configuration
LLM_PROVIDER=openai
LLM_BACKEND_URL=https://api.openai.com/v1

# API Keys
OPENAI_API_KEY=your-actual-openai-api-key-here
FINNHUB_API_KEY=your-finnhub-api-key-here

# Agent Configuration
MAX_DEBATE_ROUNDS=1
ONLINE_TOOLS=True
```

**Benefits of OpenAI:**

- ✅ No local setup required
- ✅ Higher quality responses (GPT-4o)
- ✅ Faster startup (no model downloads)
- ✅ No GPU/CPU requirements
- ❌ Requires API costs ($0.01-0.05 per query)

#### Option B: Local Ollama Configuration (Free)

Edit your `.env` file and set:

```env
# LLM Provider Configuration
LLM_PROVIDER=ollama
LLM_BACKEND_URL=http://ollama:11434/v1

# Local Models
LLM_DEEP_THINK_MODEL=llama3.2
LLM_QUICK_THINK_MODEL=qwen3
LLM_EMBEDDING_MODEL=nomic-embed-text

# API Keys (still need Finnhub for market data)
FINNHUB_API_KEY=your-finnhub-api-key-here

# Agent Configuration
MAX_DEBATE_ROUNDS=1
ONLINE_TOOLS=True
```

**Benefits of Ollama:**

- ✅ Completely free after setup
- ✅ Data privacy (runs locally)
- ✅ Works offline
- ❌ Requires initial setup and model downloads
- ❌ Slower responses than cloud APIs

### Step 3: Run with Docker Compose

Choose the appropriate method based on your LLM provider configuration:

#### Option A: Running with OpenAI

```bash
# Build the app container
docker compose --profile openai build
# Or use optimized build: ./build.sh

# Test OpenAI connection (optional)
docker compose --profile openai run --rm app-openai python tests/test_openai_connection.py

# Run the trading agents
docker compose --profile openai run -it app-openai
```

**No additional services needed** - the app connects directly to OpenAI's API.

#### Option B: Running with Ollama (CPU)

```bash
# Start the Ollama service
docker compose --profile ollama up -d --build
# Or use optimized build: ./build.sh

# Initialize Ollama models (first time only)
# Linux/macOS:
./init-ollama.sh
# Windows Command Prompt:
init-ollama.bat

# Test Ollama connection (optional)
docker compose --profile ollama exec app-ollama python tests/test_ollama_connection.py

# Run the trading agents
docker compose --profile ollama run -it app-ollama
```

#### Option C: Running with Ollama (GPU)

First, uncomment the GPU configuration in docker-compose.yml:

```yaml
# deploy:
#   resources:
#     reservations:
#       devices:
#         - capabilities: ["gpu"]
```

Then run:

```bash
# Start with GPU support
docker compose --profile ollama up -d --build
# Or use optimized build: ./build.sh

# Initialize Ollama models (first time only)
# Linux/macOS:
./init-ollama.sh
# Windows Command Prompt:
init-ollama.bat

# Run the trading agents
docker compose --profile ollama run -it app-ollama
```

#### View Logs

To view the application logs in real-time, you can run:

```bash
docker compose --profile ollama logs -f
```

#### Stop the Containers

To stop and remove the containers:

```bash
docker compose --profile ollama down
```

### Step 4: Verify Your Setup (Optional)

#### For OpenAI Setup:

```bash
# Test OpenAI API connection
docker compose --profile openai run --rm app-openai python tests/test_openai_connection.py

# Run a quick trading analysis test
docker compose --profile openai run --rm app-openai python tests/test_setup.py

# Run all tests automatically
docker compose --profile openai run --rm app-openai python tests/run_tests.py
```

#### For Ollama Setup:

```bash
# Test Ollama connection
docker compose --profile ollama exec app-ollama python tests/test_ollama_connection.py

# Run a quick trading analysis test
docker compose --profile ollama exec app-ollama python tests/test_setup.py

# Run all tests automatically
docker compose --profile ollama exec app-ollama python tests/run_tests.py
```

### Step 5: Model Management (Optional)

#### View and Manage Models

```bash
# List all available models
docker compose --profile ollama exec ollama ollama list

# Check model cache size
du -sh ./ollama_data

# Pull additional models (cached locally)
docker compose --profile ollama exec ollama ollama pull llama3.2

# Remove a model (frees up cache space)
docker compose --profile ollama exec ollama ollama rm model-name
```

#### Model Cache Benefits

- **Persistence**: Models downloaded once are reused across container restarts
- **Speed**: Subsequent startups are much faster (seconds vs minutes)
- **Bandwidth**: No need to re-download multi-GB models
- **Offline**: Once cached, models work without internet connection

#### Troubleshooting Cache Issues

```bash
# If models seem corrupted, clear cache and re-initialize
docker compose --profile ollama down
rm -rf ./ollama_data
docker compose --profile ollama up -d
# Linux/macOS:
./init-ollama.sh
# Windows Command Prompt:
init-ollama.bat
```

✅ **Expected Output:**

```
Testing Ollama connection:
  Backend URL: http://localhost:11434/v1
  Model: qwen3:0.6b
  Embedding Model: nomic-embed-text
✅ Ollama API is responding
✅ Model 'qwen3:0.6b' is available
✅ OpenAI-compatible API is working
   Response: ...
```

---

## Alternative Method: Using `docker` Only

If you prefer not to use `docker-compose`, you can build and run the container manually.

**1. Build the Docker Image:**

```bash
# Standard build
docker build -t trading-agents .

# Or optimized build (recommended)
# Linux/macOS:
./build.sh
# Windows Command Prompt:
build.bat
```

**2. Test local ollama setup (Optional):**
Make sure you have a `.env` file configured as described in Step 2. If you are using `LLM_PROVIDER="ollama"`, you can verify that the Ollama server is running correctly and has the necessary models.

```bash
docker run -it --network host --env-file .env trading-agents python tests/test_ollama_connection.py
```

for picking environment settings from .env file. You can pass values directly using:

```bash
docker run -it --network host \
  -e LLM_PROVIDER="ollama" \
  -e LLM_BACKEND_URL="http://localhost:11434/v1" \
  -e LLM_DEEP_THINK_MODEL="qwen3:0.6b" \
  -e LLM_EMBEDDING_MODEL="nomic-embed-text"\
  trading-agents \
  python tests/test_ollama_connection.py
```

To prevent re-downloading of Ollama models, mount folder from your host and run as

```bash
docker run -it --network host \
  -e LLM_PROVIDER="ollama" \
  -e LLM_BACKEND_URL="http://localhost:11434/v1" \
  -e LLM_DEEP_THINK_MODEL="qwen3:0.6b" \
  -e LLM_EMBEDDING_MODEL="nomic-embed-text"\
  -v ./ollama_cache:/app/.ollama \
  trading-agents \
  python tests/test_ollama_connection.py
```

**3. Run the Docker Container:**
Make sure you have a `.env` file configured as described in Step 2.

```bash
docker run --rm -it \
  --network host \
  --env-file .env \
  -v ./data:/app/data \
  --name trading-agents \
  trading-agents
```

**4. Run on GPU machine:**
For running on GPU machine, pass `--gpus=all` flag to the `docker run` command:

```bash
docker run --rm -it \
  --gpus=all \
  --network host \
  --env-file .env \
  -v ./data:/app/data \
  --name trading-agents \
  trading-agents
```

## Configuration Details

### Test Suite Organization

All test scripts are organized in the `tests/` directory:

```
tests/
├── __init__.py                    # Python package initialization
├── run_tests.py                   # Automated test runner
├── test_openai_connection.py      # OpenAI API connectivity tests
├── test_ollama_connection.py      # Ollama connectivity tests
└── test_setup.py                  # General setup and configuration tests
```

**Automated Testing:**

```bash
# Run all tests automatically (detects provider) - from project root
python tests/run_tests.py

# Run specific test - from project root
python tests/test_openai_connection.py
python tests/test_ollama_connection.py
python tests/test_setup.py
```

**⚠️ Important**: When running tests locally (outside Docker), always run from the **project root directory**, not from inside the `tests/` folder. The Docker commands automatically handle this.

### Live Reloading

The `app` directory is mounted as a volume into the container. This means any changes you make to the source code on your local machine will be reflected instantly in the running container without needing to rebuild the image.

### Persistent Data & Model Caching

The following volumes are used to persist data between container runs:

- **`./data`**: Stores application data, trading reports, and cached market data
- **`./ollama_data`**: Caches downloaded Ollama models (typically 1-4GB per model)

#### Model Cache Management

The Ollama models are automatically cached in `./ollama_data/` on your host machine:

- **First run**: Models are downloaded automatically (may take 5-15 minutes depending on internet speed)
- **Subsequent runs**: Models are reused from cache, startup is much faster
- **Cache location**: `./ollama_data/` directory in your project folder
- **Cache size**: Typically 2-6GB total for the required models

```bash
# Check cache size
du -sh ./ollama_data

# Clean cache if needed (will require re-downloading models)
rm -rf ./ollama_data

# List cached models
docker compose --profile ollama exec ollama ollama list
```

### GPU troubleshooting

If you find model is running very slow on GPU machine, make sur you the latest GPU drivers installed and GPU is working fine with docker. Eg you can check for Nvidia GPUs by running:

```bash
docker run --rm -it --gpus=all nvcr.io/nvidia/k8s/cuda-sample:nbody nbody -gpu -benchmark

or

nvidia-smi
```
