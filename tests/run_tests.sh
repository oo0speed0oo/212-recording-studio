#!/bin/bash
# Runs every offline test (no microphone, no browser, nothing installed).
#   tests/run_tests.sh
cd "$(dirname "$0")/.." || exit 1
PY="$HOME/kokoro-env/bin/python"
JSC=/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc
fail=0

echo "== making synthetic test audio"
"$PY" -I tests/make_test_audio.py || fail=1

echo "== voice detector (synthetic audio)"
"$JSC" tests/test_detector.js -- tests/audio | tail -3 || fail=1
"$JSC" tests/test_detector.js -- tests/audio > /dev/null || fail=1

echo "== teleprompter logic (Redo / Skip / click-to-redo / range)"
"$JSC" tests/test_prompter.js | tail -1 || fail=1
"$JSC" tests/test_prompter.js > /dev/null || fail=1

echo "== server (lists, saving audio, safety)"
"$PY" -I tests/test_server.py 2>&1 | tail -4
"$PY" -I tests/test_server.py > /dev/null 2>&1 || fail=1

echo
if [ $fail -eq 0 ]; then echo "ALL TESTS PASSED"; else echo "SOME TESTS FAILED"; fi
exit $fail
