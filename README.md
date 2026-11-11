# nil-web

A server-side web framework for the [Matrix](https://github.com/exec-software/exec-nil-framework)
language, written in Matrix.

It is a rewrite of NyWeb, an ASPX-inspired framework written in Ny against a
much older NIL framework. The design survived the move; almost none of the code
did, because the languages differ in ways that reach the architecture. See
[Differences from NyWeb](#differences-from-nyweb).

## Status

The HTTP core is complete and tested — 153 checks, all passing.

| Module | What it does | State |
| --- | --- | --- |
| `nilweb.text.scan` | splitting and folding over `array of text` | done |
| `nilweb.http.method` | the request methods, parsed and spelled | done |
| `nilweb.http.status` | status codes, reason phrases, body rules | done |
| `nilweb.http.headers` | case-insensitive fields that keep order and repeats | done |
| `nilweb.http.uri` | percent coding, query strings, form bodies | done (one documented limit) |
| `nilweb.http.request` | parsing a whole request message | done |
| `nilweb.http.response` | building and rendering a response | done |
| `nilweb.routing.router` | patterns, captures, wildcards, 404 vs 405 | done |
| templates | `.nilhtml` rendering with layouts and partials | not started |
| sessions | signed cookies and a server-side store | not started |
| static files | serving a directory with caching validators | not started |
| middleware | a request/response chain | not started |
| **transport** | **accepting a connection** | **blocked — see below** |

```sh
./scripts/test.sh    # 153 checks
./scripts/demo.sh    # requests in, responses out
```

Both take `NIL_SDK` if the framework is not a sibling checkout.

## There is no server in here yet

The NIL runtime has no networking. Not "an unfinished API" — no network
intrinsics at all. The runtime's intrinsic table covers files, paths,
directories, databases, the environment, the console, regular expressions and
GPU compute, and nothing that opens a socket. `nil.std.net` says so itself:

> Transport-independent HTTP protocol values. Network I/O is intentionally
> absent from this active module until an HTTP provider is built and tested.

Sockets and an HTTP server exist as Ny designs under `nilstd/experimental/net`,
and a `socket.linux.c` sits in `nilstd/native/net`, but nothing builds them and
no `ml_net` library is produced. The NyWeb original did not have this problem
because it sat on `nyhttp`, which has no NIL equivalent.

So this framework is, for now, exactly what `nil.std.net` is: everything above
the transport. `answer` in `src/main.matrix` takes a request as text and gives
back a response as text. Whatever ends up accepting connections calls that.

Three ways to get a transport, in the order I would pick them:

1. **`net.*` intrinsics in the runtime.** How `file.*` already works, uses the
   `socket.linux.c` that is already there, and unblocks the stdlib's own
   pending HTTP provider rather than only this repo. Needs a change to
   exec-nil-framework and a rebuild.
2. **An Ny shim over a C library shipped here.** Matrix cannot declare FFI —
   `note interop` is rejected with "a bodyless paragraph is an intrinsic-backed
   declaration, which is not lowered yet" — but an Ny page can, and a Matrix
   book can read that page. This is proven working: Matrix → Ny `@interop` →
   `dlopen`, with text crossing in both directions.
3. **Stay transport-independent** and let something else do the accepting.

## Differences from NyWeb

**Code-behind is gone.** NyWeb gave every page a class implementing `IPage` and
the router called it. Matrix has no capturing closures, and an array of class
handles compiles but does not execute, so a callable cannot be stored in a route
table. A route names its handler with text and the application switches on the
name. The cost is one `switch` per application; what it buys is a route table
that is plain data — readable, testable and reorderable without running it.

**The template engine will be Matrix, not C++.** NyWeb's was 626 lines of C++
reached by FFI. Matrix can do the string work itself, and a template engine in
the language it serves is one that can be read by the people using it.

**Everything is a value.** A response is a record; middleware takes one and
gives back another. Nothing mutates a shared request object, which is what makes
a handler testable without a server.

**Parsing happens once.** NyWeb re-split the query string on every lookup, so a
page reading five parameters parsed the query five times. Here a request is
parsed at construction and read from arrays afterwards.

## A known limit in percent-encoding

`decode-component` is correct for all of Unicode: overlong encodings, surrogate
halves, truncated sequences and four-byte planes are all handled, and ill-formed
input becomes U+FFFD as the WHATWG standard requires.

`encode-component` is correct for ASCII and passes non-ASCII bytes through
unescaped. Escaping one means reading its numeric value, and nothing reachable
from Matrix can do that: `nil.std.text` slices by byte but has no byte-to-number
conversion, and `nil.std.core`'s `char_to_i32` takes a whole Unicode scalar and
**aborts the process** on a partial one — a fatal runtime error, not a fault
that `attempt` can catch. So a byte of a multi-byte character cannot be
inspected, and such a character cannot be isolated to inspect whole, because
isolating it needs the same test.

A `byte_at` in `nil.std.text` would close this in an afternoon. It is tracked
here rather than worked around, because every workaround available is wrong in
a way that only shows up on someone's name.

## Layout

```text
src/nilweb/text/scan.matrix        text splitting and folding
src/nilweb/http/method.matrix      request methods
src/nilweb/http/status.matrix      status codes and reason phrases
src/nilweb/http/headers.matrix     header fields
src/nilweb/http/uri.matrix         percent coding and query strings
src/nilweb/http/request.matrix     the inbound request
src/nilweb/http/response.matrix    the outbound response
src/nilweb/routing/router.matrix   route matching
src/main.matrix                    a demonstration, and the `answer` entry point
tests/run-tests.matrix             every suite
tests/nilweb/tests/harness.matrix  the checks
```

A book lives at the path its name spells: `book nilweb.http.request` is
`src/nilweb/http/request.matrix`.

## License

MIT.
