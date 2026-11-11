#!/bin/sh
# Run the demonstration application as a real server.
#
#   ./scripts/serve.sh            # 127.0.0.1:8080
#   NILWEB_PORT=9000 ./scripts/serve.sh
#
# One connection at a time: there are no threads in the language to hand a
# connection to. Fine for development and behind a proxy; see the README.
set -e
SDK="${NIL_SDK:-$(cd "$(dirname "$0")/../../exec-nil-framework/sdk" && pwd)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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
# The templates and public files are found relative to the working directory.
cd "$ROOT"
NILWEB_SERVE=1 exec "$SDK/nilinterpreter" "$OUT/nil-web.nil"
