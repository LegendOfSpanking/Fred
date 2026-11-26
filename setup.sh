#!/usr/bin/env bash
# Fred's World - one-time installer + launcher
set -e
cd "$(dirname "$0")"
echo "Installing Fred's tools..."
python3 -m pip install --quiet --upgrade flask google-genai
printf "config.json\nstate.json\nmemory.json\n*.backup.html\n*.original.html\n__pycache__/\n" > .gitignore
echo "Starting Fred's World on port 8000. Open the forwarded port in Codespaces (Ports tab)."
python3 server.py
