#!/bin/bash
# Tests start.command with pretend Pythons in a throwaway folder: it must pick the right Python, ask before installing anything,
# install nothing when you say no, and never touch the real Mac. (No Python or numpy is installed by this test.)
#   tests/test_start.sh
HERE="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/studio-start-test.XXXXXX")"
T="$(cd "$T" && pwd)"                 # one clean spelling of the path (no double slashes)
trap 'rm -rf "$T"' EXIT
pass=0; failn=0
check() { if [ "$2" = "yes" ]; then pass=$((pass+1)); echo "  PASS  $1"; else failn=$((failn+1)); echo "  FAIL  $1"; fi; }
has() { case "$1" in *"$2"*) echo yes;; *) echo no;; esac; }

new_studio_dir() {                    # a copy of start.command next to a pretend studio.py
  D="$T/$1"; rm -rf "$D"; mkdir -p "$D"
  cp "$HERE/start.command" "$D/start.command"; echo "# pretend" > "$D/studio.py"
}
# A pretend python: `-c ...` (the numpy check) succeeds only when $NUMPY is 1; `-m venv DIR` makes a pretend venv python in DIR;
# anything else is "running the studio": it prints RAN and its arguments.
make_python() {                       # make_python <path>
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<'PYEOF'
#!/bin/bash
SELF="$0"
if [ "$1" = "-c" ]; then [ "$NUMPY" = "1" ] && exit 0; exit 1; fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'VEOF'
#!/bin/bash
D="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$1" = "-c" ]; then [ -f "$D/numpy-installed" ] && exit 0; exit 1; fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then touch "$D/numpy-installed"; echo "PIP: $*" >> "$D/pip.log"; exit 0; fi
echo "RAN-VENV: $*"; exit ${EXIT_CODE:-0}
VEOF
  chmod +x "$3/bin/python"; echo "VENV: $*" >> "$(dirname "$SELF")/venv.log"; exit 0
fi
echo "RAN: $*"; exit ${EXIT_CODE:-0}
PYEOF
  chmod +x "$1"
}
FB="$T/fakebin"; mkdir -p "$FB"
for tool in dirname mkdir chmod touch cat; do ln -s "$(command -v $tool)" "$FB/$tool"; done
printf '#!/bin/bash\necho "OPENED: $*" >> "%s/open.log"\n' "$T" > "$FB/open"; chmod +x "$FB/open"
run() { # run <dir> <path> <home> <stdin> [env...]   -> output in $OUT, status in $ST
  local d="$1" path="$2" home="$3" input="$4"; shift 4
  OUT="$(cd "$T" && env -i HOME="$home" PATH="$path" "$@" /bin/bash "$d/start.command" --no-browser <<< "$input" 2>&1)"; ST=$?
}
EMPTY_HOME="$T/home"; mkdir -p "$EMPTY_HOME"

echo "== a Python with numpy is found and used"
new_studio_dir a; make_python "$T/bin-a/python3"
run "$T/a" "$T/bin-a:$FB" "$EMPTY_HOME" "" NUMPY=1
check "starts the studio with --no-browser passed on" "$(has "$OUT" "RAN: $T/a/studio.py --no-browser")"
check "exit status 0" "$([ $ST -eq 0 ] && echo yes || echo no)"
check "nothing installed (no .venv)" "$([ ! -e "$T/a/.venv" ] && echo yes || echo no)"

echo "== numpy missing and you say no: nothing is installed"
new_studio_dir b; make_python "$T/bin-b/python3"
run "$T/b" "$T/bin-b:$FB" "$EMPTY_HOME" "n" NUMPY=0
check "explains numpy is missing" "$(has "$OUT" "numpy is missing")"
check "says nothing was installed" "$(has "$OUT" "nothing was installed")"
check "stops with an error status" "$([ $ST -ne 0 ] && echo yes || echo no)"
check "no .venv folder made" "$([ ! -e "$T/b/.venv" ] && echo yes || echo no)"
check "studio not started" "$([ "$(has "$OUT" "RAN:")" = no ] && echo yes || echo no)"

echo "== numpy missing and you say y: a private .venv is made, then the studio starts"
new_studio_dir c; make_python "$T/bin-c/python3"
run "$T/c" "$T/bin-c:$FB" "$EMPTY_HOME" "y" NUMPY=0
check ".venv made inside the studio folder" "$([ -x "$T/c/.venv/bin/python" ] && echo yes || echo no)"
check "pip installed only numpy" "$([ "$(cat "$T/c/.venv/pip.log" 2>/dev/null)" = "PIP: -m pip install --quiet numpy" ] && echo yes || echo no)"
check "the studio runs with the .venv Python" "$(has "$OUT" "RAN-VENV: $T/c/studio.py --no-browser")"
echo "== next time the .venv is used without asking"
run "$T/c" "$T/bin-c:$FB" "$EMPTY_HOME" "" NUMPY=0
check "no question, studio starts" "$([ "$(has "$OUT" "Set it up now")" = no ] && [ "$(has "$OUT" "RAN-VENV")" = yes ] && echo yes || echo no)"

echo "== no Python at all: tells you what to install, opens the download page"
new_studio_dir d; rm -f "$T/open.log"
run "$T/d" "$FB" "$EMPTY_HOME" "" NUMPY=1
check "says Python is not installed" "$(has "$OUT" "Python 3 is not installed")"
check "gives the download address" "$(has "$OUT" "python.org/downloads")"
check "opened the download page" "$(has "$(cat "$T/open.log" 2>/dev/null)" "python.org/downloads")"
check "stops with an error status" "$([ $ST -ne 0 ] && echo yes || echo no)"

echo "== Mike's own Python (~/kokoro-env) is used when it is there"
new_studio_dir e; H="$T/home-e"; make_python "$H/kokoro-env/bin/python"
run "$T/e" "$FB" "$H" "" NUMPY=1
check "uses ~/kokoro-env" "$(has "$OUT" "RAN: $T/e/studio.py --no-browser")"

echo "== a problem in the studio is reported and its exit code kept"
new_studio_dir f; make_python "$T/bin-f/python3"
run "$T/f" "$T/bin-f:$FB" "$EMPTY_HOME" "" NUMPY=1 EXIT_CODE=3
check "exit code 3 kept" "$([ $ST -eq 3 ] && echo yes || echo no)"
check "friendly message" "$(has "$OUT" "stopped with a problem")"

echo "== the file itself"
check "start.command is executable" "$([ -x "$HERE/start.command" ] && echo yes || echo no)"
check "first line is a bash shebang" "$([ "$(head -1 "$HERE/start.command")" = "#!/bin/bash" ] && echo yes || echo no)"

echo
echo "$pass passed, $failn failed"
[ $failn -eq 0 ]
