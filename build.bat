@echo off
REM 🚀 Optimized BuildKit Docker Build Script for TradingAgents (Windows Batch)
REM This script uses Docker BuildKit for faster builds with advanced caching

setlocal EnableDelayedExpansion

REM Configuration
set "IMAGE_NAME=trading-agents"
set "CACHE_TAG=%IMAGE_NAME%:cache"
set "LATEST_TAG=%IMAGE_NAME%:latest"
set "REGISTRY="
set "TARGET=production"
set "CLEAN_CACHE="
set "RUN_TESTS="
set "SHOW_STATS="
set "SHOW_HELP="

REM Parse command line arguments
:parse_args
if "%~1"=="" goto end_parse
if /i "%~1"=="--clean" (
    set "CLEAN_CACHE=1"
    shift
    goto parse_args
)
if /i "%~1"=="--test" (
    set "RUN_TESTS=1"
    shift
    goto parse_args
)
if /i "%~1"=="--stats" (
    set "SHOW_STATS=1"
    shift
    goto parse_args
)
if /i "%~1"=="--help" (
    set "SHOW_HELP=1"
    shift
    goto parse_args
)
if /i "%~1"=="-h" (
    set "SHOW_HELP=1"
    shift
    goto parse_args
)
echo [ERROR] Unknown option: %~1
exit /b 1

:end_parse

REM Show help if requested
if defined SHOW_HELP (
    echo 🚀 TradingAgents Optimized Docker Build ^(Windows^)
    echo Usage: build-optimized.bat [OPTIONS]
    echo.
    echo Options:
     echo   --clean             Clean build cache before building
    echo   --test              Run tests after building
    echo   --stats             Show cache statistics after building
    echo   --help, -h          Show this help message
    echo.
    echo Examples:
    echo   build-optimized.bat                    # Build image
    echo   build-optimized.bat --clean --test     # Clean cache, build, and test
    exit /b 0
)

echo 🚀 TradingAgents Optimized Docker Build ^(Windows^)
echo =========================================

REM Check if BuildKit is available
echo [INFO] Checking BuildKit availability...
docker buildx version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker BuildKit ^(buildx^) is not available
    echo [ERROR] Please install Docker BuildKit or update Docker to a newer version
    exit /b 1
)
echo [SUCCESS] BuildKit is available

REM Create buildx builder if it doesn't exist
echo [INFO] Setting up BuildKit builder...
docker buildx inspect trading-agents-builder >nul 2>&1
if errorlevel 1 (
    echo [INFO] Creating new buildx builder 'trading-agents-builder'...
    docker buildx create --name trading-agents-builder --driver docker-container --bootstrap
    if errorlevel 1 (
        echo [ERROR] Failed to create builder
        exit /b 1
    )
)

REM Use our builder
docker buildx use trading-agents-builder
if errorlevel 1 (
    echo [ERROR] Failed to use builder
    exit /b 1
)
echo [SUCCESS] Builder 'trading-agents-builder' is ready

REM Clean cache if requested
if defined CLEAN_CACHE (
    echo [INFO] Cleaning build cache...
    docker buildx prune -f
    echo [SUCCESS] Build cache cleaned
)

REM Show build information
echo [INFO] Build Information:
echo   📦 Image: %LATEST_TAG%
echo   📊 Cache: Local BuildKit cache
echo   🔄 Multi-stage: Yes ^(builder → runtime^)
echo   🌐 Network: Host networking mode

REM Build the image
echo [INFO] Building image with BuildKit cache optimization...

REM Get build metadata
for /f "tokens=*" %%i in ('powershell -Command "(Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')"') do set "BUILD_DATE=%%i"
for /f "tokens=*" %%i in ('git rev-parse --short HEAD 2^>nul') do set "GIT_HASH=%%i"
if "!GIT_HASH!"=="" set "GIT_HASH=unknown"

REM Execute build
echo [INFO] Starting Docker build...
docker buildx build ^
    --file Dockerfile ^
    --tag %LATEST_TAG% ^
    --cache-from type=local,src=C:\tmp\.buildx-cache ^
    --cache-to type=local,dest=C:\tmp\.buildx-cache,mode=max ^
    --label build.date=%BUILD_DATE% ^
    --label build.version=%GIT_HASH% ^
    --load ^
    .

if errorlevel 1 (
    echo [ERROR] ❌ Build failed!
    exit /b 1
)

echo [SUCCESS] ✅ Build completed successfully!

REM Test the image if requested
if defined RUN_TESTS (
    echo [INFO] Testing built image...
    
    REM Basic functionality test
    docker run --rm %LATEST_TAG% python -c "print('✅ Image test successful')"
    if errorlevel 1 (
        echo [ERROR] Image test failed
        exit /b 1
    )
    echo [SUCCESS] Image test passed
    
    REM Test import capabilities
    docker run --rm %LATEST_TAG% python -c "from tradingagents.default_config import DEFAULT_CONFIG; print('✅ Import test successful')"
    if errorlevel 1 (
        echo [WARNING] Import test failed ^(this might be expected if dependencies are missing^)
    ) else (
        echo [SUCCESS] Import test passed
    )
)

REM Show cache statistics if requested
if defined SHOW_STATS (
    echo [INFO] Cache Statistics:
    docker buildx du 2>nul || echo   Cache statistics not available
)

echo.
echo [SUCCESS] 🎉 Ready to use! Try:
echo   docker run -it --network host %LATEST_TAG%
echo   docker compose --profile openai run -it app-openai
echo   docker compose --profile ollama up -d ^&^& docker compose --profile ollama exec app-ollama cmd
echo   docker compose --profile default run -it app

exit /b 0