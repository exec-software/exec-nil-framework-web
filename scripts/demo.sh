#!/bin/sh
# Compile and run the demonstration in src/main.matrix: a set of requests fed
# through the framework as text, with the rendered responses printed.
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
    -I "$STDLIB" -I "$ROOT/src" \
    --output "$OUT/nil-web.nil" "$ROOT/src/main.matrix" > /dev/null
exec "$SDK/nilinterpreter" "$OUT/nil-web.nil"
