@echo off
setlocal enabledelayedexpansion

echo 🚀 Initializing Ollama models...

REM Define required models
set DEEP_THINK_MODEL=qwen3:0.6b
set EMBEDDING_MODEL=nomic-embed-text

REM Wait for Ollama to be ready
echo ⏳ Waiting for Ollama service to start...
set max_attempts=30
set attempt=0

:wait_loop
if %attempt% geq %max_attempts% goto timeout_error

docker compose --profile ollama exec ollama ollama list >nul 2>&1
if %errorlevel% equ 0 (
    echo ✅ Ollama is ready!
    goto ollama_ready
)

set /a attempt=%attempt%+1
echo    Waiting for Ollama... (attempt %attempt%/%max_attempts%)
timeout /t 2 /nobreak >nul
goto wait_loop

:timeout_error
echo ❌ Error: Ollama failed to start within the expected time
exit /b 1

:ollama_ready

REM Check cache directory
if exist ".\ollama_data" (
    echo 📁 Found existing ollama_data cache directory
    for /f "tokens=*" %%a in ('dir ".\ollama_data" /s /-c ^| find "bytes"') do (
        echo    Cache directory exists
    )
) else (
    echo 📁 Creating ollama_data cache directory...
    mkdir ".\ollama_data"
)

REM Get list of currently available models
echo 🔍 Checking for existing models...
docker compose --profile ollama exec ollama ollama list > temp_models.txt 2>nul
if %errorlevel% neq 0 (
    echo > temp_models.txt
)

REM Check if deep thinking model exists
findstr /c:"%DEEP_THINK_MODEL%" temp_models.txt >nul
if %errorlevel% equ 0 (
    echo ✅ Deep thinking model '%DEEP_THINK_MODEL%' already available
) else (
    echo 📥 Pulling deep thinking model: %DEEP_THINK_MODEL%...
    docker compose --profile ollama exec ollama ollama pull %DEEP_THINK_MODEL%
    if %errorlevel% equ 0 (
        echo ✅ Model %DEEP_THINK_MODEL% pulled successfully
    ) else (
        echo ❌ Failed to pull model %DEEP_THINK_MODEL%
        goto cleanup
    )
)

REM Check if embedding model exists
findstr /c:"%EMBEDDING_MODEL%" temp_models.txt >nul
if %errorlevel% equ 0 (
    echo ✅ Embedding model '%EMBEDDING_MODEL%' already available
) else (
    echo 📥 Pulling embedding model: %EMBEDDING_MODEL%...
    docker compose --profile ollama exec ollama ollama pull %EMBEDDING_MODEL%
    if %errorlevel% equ 0 (
        echo ✅ Model %EMBEDDING_MODEL% pulled successfully
    ) else (
        echo ❌ Failed to pull model %EMBEDDING_MODEL%
        goto cleanup
    )
)

REM List all available models
echo 📋 Available models:
docker compose --profile ollama exec ollama ollama list

REM Show cache info
if exist ".\ollama_data" (
    echo 💾 Model cache directory: .\ollama_data
)

echo 🎉 Ollama initialization complete!
echo 💡 Tip: Models are cached in .\ollama_data and will be reused on subsequent runs

:cleanup
if exist temp_models.txt del temp_models.txt
exit /b 0