#!/bin/bash
# Double-click this file to start the 212 Recording Studio (Mac).
# It finds Python, starts the studio and opens it in your browser. Close this window (or press Ctrl+C) to stop.
# It installs nothing unless you say "y" when it asks.

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"

# A Python is "good" when it has numpy (the studio needs it) and is version 3.8 or newer.
good() {
  [ -n "$1" ] && [ -x "$1" ] && "$1" -c "import sys, numpy; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >/dev/null 2>&1
}

# macOS has a stand-in /usr/bin/python3 that pops up an "install developer tools" window when Python is missing.
# Do not touch it unless the tools are really there.
real_python3() {
  local p
  p="$(command -v python3 2>/dev/null)"
  [ -z "$p" ] && return 1
  if [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then return 1; fi
  echo "$p"
}

PY=""
for cand in "$STUDIO_PYTHON" "$HERE/.venv/bin/python" "$HOME/kokoro-env/bin/python"; do
  if good "$cand"; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  P3="$(real_python3)"
  if good "$P3"; then PY="$P3"; fi
fi

if [ -z "$PY" ]; then
  echo
  echo "  212 Recording Studio needs Python 3 with one extra piece called numpy."
  echo
  P3="$(real_python3)"
  if [ -z "$P3" ]; then
    echo "  Python 3 is not installed on this Mac."
    echo "  1. Install it from  https://www.python.org/downloads/  (the big yellow button, then open the file it downloads)."
    echo "  2. Then double-click start.command again."
    echo
    command -v open >/dev/null 2>&1 && open "https://www.python.org/downloads/" >/dev/null 2>&1
    read -n 1 -s -r -p "  Press any key to close this window."
    echo
    exit 1
  fi
  echo "  Python is here ($P3), but numpy is missing."
  echo "  I can set it up for you now. This makes a private folder called .venv inside the studio folder and"
  echo "  installs numpy into it (about 15 MB, needs the internet). Nothing else on your Mac is changed."
  echo
  read -r -p "  Set it up now? (y/n) " ANS
  case "$ANS" in
    y|Y|yes|YES)
      echo
      if "$P3" -m venv "$HERE/.venv" && "$HERE/.venv/bin/python" -m pip install --quiet numpy; then
        echo "  Done."
        PY="$HERE/.venv/bin/python"
      else
        echo
        echo "  That did not work (is the internet on?). Nothing was changed except the .venv folder, which you can delete."
        read -n 1 -s -r -p "  Press any key to close this window."
        echo
        exit 1
      fi
      ;;
    *)
      echo
      echo "  OK, nothing was installed. When you are ready, double-click start.command again and answer y."
      read -n 1 -s -r -p "  Press any key to close this window."
      echo
      exit 1
      ;;
  esac
fi

"$PY" "$HERE/studio.py" "$@"
STATUS=$?
if [ $STATUS -ne 0 ] && [ $STATUS -ne 130 ] && [ $STATUS -ne 143 ]; then      # 130/143 = you stopped it yourself
  echo
  echo "  The studio stopped with a problem (code $STATUS). The message above says why."
  read -n 1 -s -r -p "  Press any key to close this window."
  echo
fi
exit $STATUS
