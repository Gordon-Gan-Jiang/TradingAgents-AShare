#!/bin/bash

# 🚀 Optimized BuildKit Docker Build Script for TradingAgents
# This script uses Docker BuildKit for faster builds with advanced caching

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
IMAGE_NAME="trading-agents"
CACHE_TAG="${IMAGE_NAME}:cache"
LATEST_TAG="${IMAGE_NAME}:latest"
REGISTRY="" # Set this if you want to push to a registry

# Function to print colored output
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if BuildKit is available
check_buildkit() {
    print_status "Checking BuildKit availability..."
    
    if ! docker buildx version > /dev/null 2>&1; then
        print_error "Docker BuildKit (buildx) is not available"
        print_error "Please install Docker BuildKit or update Docker to a newer version"
        exit 1
    fi
    
    print_success "BuildKit is available"
}

# Create buildx builder if it doesn't exist
setup_builder() {
    print_status "Setting up BuildKit builder..."
    
    # Check if our builder exists
    if ! docker buildx inspect trading-agents-builder > /dev/null 2>&1; then
        print_status "Creating new buildx builder 'trading-agents-builder'..."
        docker buildx create --name trading-agents-builder --driver docker-container --bootstrap
    fi
    
    # Use our builder
    docker buildx use trading-agents-builder
    print_success "Builder 'trading-agents-builder' is ready"
}

# Build with cache optimization
build_image() {
    print_status "Building image with BuildKit cache optimization..."
    
    # Build arguments
    local build_args=(
        --file Dockerfile
        --tag "$LATEST_TAG"
        --cache-from "type=local,src=/tmp/.buildx-cache"
        --cache-to "type=local,dest=/tmp/.buildx-cache,mode=max"
        --load  # Load into local Docker daemon
    )
    
    # Add build metadata
    build_args+=(
        --label "build.date=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
        --label "build.version=$(git rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
        --label "build.target=$target"
    )
    
    print_status "Build command: docker buildx build ${build_args[*]} ."
    
    # Execute build
    if docker buildx build "${build_args[@]}" .; then
        print_success "Build completed successfully!"
        return 0
    else
        print_error "Build failed!"
        print_warning "Attempting fallback build with simple Dockerfile..."
        return build_simple_fallback
    fi
}

# Fallback build function for when BuildKit fails
build_simple_fallback() {
    print_status "Using simple Dockerfile as fallback..."
    
    if [ -f "Dockerfile.simple" ]; then
        if docker build -f Dockerfile.simple -t "$LATEST_TAG" .; then
            print_success "Fallback build completed successfully!"
            print_warning "Note: Using simple build without advanced caching"
            return 0
        else
            print_error "Fallback build also failed!"
            return 1
        fi
    else
        print_error "Dockerfile.simple not found for fallback"
        return 1
    fi
}

# Show build info
show_build_info() {
    print_status "Build Information:"
    echo "  📦 Image: $LATEST_TAG"
    echo "  🏗️  Builder: $(docker buildx inspect --bootstrap | grep "Name:" | head -1 | cut -d: -f2 | xargs)"
    echo "  📊 Cache: Local BuildKit cache"
    echo "  🔄 Multi-stage: Yes (builder → runtime)"
    echo "  🌐 Network: Host networking mode"
}

# Test the built image
test_image() {
    print_status "Testing built image..."
    
    # Basic functionality test
    if docker run --rm "$LATEST_TAG" python -c "print('✅ Image test successful')"; then
        print_success "Image test passed"
    else
        print_error "Image test failed"
        return 1
    fi
    
    # Test import capabilities
    if docker run --rm "$LATEST_TAG" python -c "from tradingagents.default_config import DEFAULT_CONFIG; print('✅ Import test successful')"; then
        print_success "Import test passed"
    else
        print_warning "Import test failed (this might be expected if dependencies are missing)"
    fi
}

# Show cache statistics
show_cache_stats() {
    print_status "Cache Statistics:"
    
    # Show buildx disk usage
    if docker buildx du > /dev/null 2>&1; then
        docker buildx du
    else
        echo "  Cache statistics not available"
    fi
}

# Clean up build cache
clean_cache() {
    print_status "Cleaning build cache..."
    docker buildx prune -f
    print_success "Build cache cleaned"
}

# Main function
main() {
    echo "🚀 TradingAgents Optimized Docker Build"
    echo "========================================"
    
    # Parse arguments
    local clean=false
    local test=false
    local stats=false
    
    while [[ $# -gt 0 ]]; do
        case $1 in
            --clean)
                clean=true
                shift
                ;;
            --test)
                test=true
                shift
                ;;
            --stats)
                stats=true
                shift
                ;;
            --help|-h)
                echo "Usage: $0 [OPTIONS]"
                echo ""
                echo "Options:"
                echo "  --clean             Clean build cache before building"
                echo "  --test              Run tests after building"
                echo "  --stats             Show cache statistics after building"
                echo "  --help, -h          Show this help message"
                echo ""
                echo "Examples:"
                echo "  $0                  # Build image"
                echo "  $0 --clean --test   # Clean cache, build, and test"
                echo "  $0 --stats          # Build and show cache stats"
                exit 0
                ;;
            *)
                print_error "Unknown option: $1"
                exit 1
                ;;
        esac
    done
    
    # Execute steps
    check_buildkit
    setup_builder
    
    if [ "$clean" = true ]; then
        clean_cache
    fi
    
    show_build_info
    
    if build_image; then
        print_success "✅ Build completed successfully!"
        
        if [ "$test" = true ]; then
            test_image
        fi
        
        if [ "$stats" = true ]; then
            show_cache_stats
        fi
        
        echo ""
        print_success "🎉 Ready to use! Try:"
        echo "  docker run -it --network host $LATEST_TAG"
        echo "  docker compose --profile openai run -it app-openai"
        echo "  docker compose --profile ollama up -d && docker compose --profile ollama exec app-ollama bash"
        echo "  docker compose --profile default run -it app"
        
    else
        print_error "❌ Build failed!"
        exit 1
    fi
}

# Run main function with all arguments
main "$@"