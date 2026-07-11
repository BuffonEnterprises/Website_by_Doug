#!/usr/bin/env bash
# Liga o Jarvis v2.  Uso: ./run.sh   |   ./run.sh --text   |   ./run.sh --mode ptt
cd "$(dirname "$0")" || exit 1
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi
exec python3 jarvis.py "$@"
