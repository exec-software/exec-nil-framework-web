#!/bin/sh
# Compile and run the test suite. The exit code is the verdict.
#
# NIL_SDK points at an exec-nil-framework SDK directory (the one holding
# nilcompiler, nilinterpreter and nilstd/). It defaults to the sibling
# checkout, which is where it is during development.
set -e
SDK="${NIL_SDK:-$(cd "$(dirname "$0")/../../exec-nil-framework/sdk" && pwd)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# A published SDK stages the standard library at nilstd/; a framework checkout
# keeps it at nilstd/src. Take whichever is there rather than making the caller
# know which kind of tree NIL_SDK points at.
if [ -d "$SDK/nilstd/src" ]; then
    STDLIB="$SDK/nilstd/src"
else
    STDLIB="$SDK/nilstd"
fi
OUT="${ROOT}/build"
mkdir -p "$OUT"
"$SDK/nilcompiler" --no-bundle \
    -I "$STDLIB" -I "$ROOT/src" -I "$ROOT/tests" \
    --output "$OUT/tests.nil" "$ROOT/tests/run-tests.matrix" > /dev/null
exec "$SDK/nilinterpreter" "$OUT/tests.nil"
