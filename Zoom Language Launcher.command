#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3 is required. Install Python 3 and try again.'
  read -r -p 'Press Enter to close.'
  exit 1
fi
exec python3 zoom_language_launcher.py
