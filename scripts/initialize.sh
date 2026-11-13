#!/bin/bash
# Initializes a new worktree with necessary environment setup.

set -eo pipefail

cd "$(dirname "$0")/.."

# Check for UV
if ! command -v uv &> /dev/null; then
    echo "Error: UV is not installed."
    echo "Install with: brew install uv"
    echo "Or: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "Setting up Python virtual environment with UV..."
uv venv .venv
uv pip install -e ".[dev]"

echo "Installing Node.js dependencies..."
npm install

echo "Worktree initialized!"
