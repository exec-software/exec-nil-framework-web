#!/bin/sh
# Compile and run the demonstration in src/main.matrix: a set of requests fed
# through the framework as text, with the rendered responses printed.
set -e
SDK="${NIL_SDK:-$(cd "$(dirname "$0")/../../exec-nil-framework/sdk" && pwd)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${ROOT}/build"
mkdir -p "$OUT"
"$SDK/nilcompiler" --no-bundle \
    -I "$SDK/nilstd/src" -I "$ROOT/src" \
    --output "$OUT/nil-web.nil" "$ROOT/src/main.matrix" > /dev/null
exec "$SDK/nilinterpreter" "$OUT/nil-web.nil"
