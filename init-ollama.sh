#!/bin/bash
set -e

echo "🚀 Initializing Ollama models..."

# Define required models
DEEP_THINK_MODEL="qwen3:0.6b"
EMBEDDING_MODEL="nomic-embed-text"

# Wait for Ollama to be ready
echo "⏳ Waiting for Ollama service to start..."
max_attempts=30
attempt=0
while [ $attempt -lt $max_attempts ]; do
    if docker compose --profile ollama exec ollama ollama list > /dev/null 2>&1; then
        echo "✅ Ollama is ready!"
        break
    fi
    echo "   Waiting for Ollama... (attempt $((attempt + 1))/$max_attempts)"
    sleep 2
    attempt=$((attempt + 1))
done

if [ $attempt -eq $max_attempts ]; then
    echo "❌ Error: Ollama failed to start within the expected time"
    exit 1
fi

# Check cache directory
if [ -d "./ollama_data" ]; then
    echo "📁 Found existing ollama_data cache directory"
    cache_size=$(du -sh ./ollama_data 2>/dev/null | cut -f1 || echo "0")
    echo "   Cache size: $cache_size"
else
    echo "📁 Creating ollama_data cache directory..."
    mkdir -p ./ollama_data
fi

# Get list of currently available models
echo "🔍 Checking for existing models..."
available_models=$(docker compose --profile ollama exec ollama ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' || echo "")

# Function to check if model exists
model_exists() {
    local model_name="$1"
    echo "$available_models" | grep -q "^$model_name"
}

# Pull deep thinking model if not present
if model_exists "$DEEP_THINK_MODEL"; then
    echo "✅ Deep thinking model '$DEEP_THINK_MODEL' already available"
else
    echo "📥 Pulling deep thinking model: $DEEP_THINK_MODEL..."
    docker compose --profile ollama exec ollama ollama pull "$DEEP_THINK_MODEL"
    echo "✅ Model $DEEP_THINK_MODEL pulled successfully"
fi

# Pull embedding model if not present
if model_exists "$EMBEDDING_MODEL"; then
    echo "✅ Embedding model '$EMBEDDING_MODEL' already available"
else
    echo "📥 Pulling embedding model: $EMBEDDING_MODEL..."
    docker compose --profile ollama exec ollama ollama pull "$EMBEDDING_MODEL"
    echo "✅ Model $EMBEDDING_MODEL pulled successfully"
fi

# List all available models
echo "📋 Available models:"
docker compose --profile ollama exec ollama ollama list

# Show cache info
if [ -d "./ollama_data" ]; then
    cache_size=$(du -sh ./ollama_data 2>/dev/null | cut -f1 || echo "unknown")
    echo "💾 Model cache size: $cache_size (stored in ./ollama_data)"
fi

echo "🎉 Ollama initialization complete!"
echo "💡 Tip: Models are cached in ./ollama_data and will be reused on subsequent runs"