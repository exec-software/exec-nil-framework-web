# nil-web

A server-side web framework for the [Matrix](https://github.com/exec-software/exec-nil-framework)
language, written in Matrix.

It is a rewrite of NyWeb, an ASPX-inspired framework written in Ny against a
much older NIL framework. The design survived the move; almost none of the code
did, because the languages differ in ways that reach the architecture. See
[Differences from NyWeb](#differences-from-nyweb).

## Status

Built and tested end to end — 272 checks, all passing, and a server that
serves.

| Module | What it does | State |
| --- | --- | --- |
| `nilweb.text.scan` | splitting and folding over `array of text` | done |
| `nilweb.http.method` | the request methods, parsed and spelled | done |
| `nilweb.http.status` | status codes, reason phrases, body rules | done |
| `nilweb.http.headers` | case-insensitive fields that keep order and repeats | done |
| `nilweb.http.uri` | percent coding, query strings, form bodies | done |
| `nilweb.http.request` | parsing a whole request message | done |
| `nilweb.http.response` | building and rendering a response | done |
| `nilweb.routing.router` | patterns, captures, wildcards, 404 vs 405 | done |
| `nilweb.template.context` | the data a template may read | done |
| `nilweb.template.render` | `.nilhtml` with layouts, partials, loops, escaping | done |
| `nilweb.session.session` | a server-side store, secure ids, cookie attributes | done |
| `nilweb.static.files` | serving a directory, with the traversal checks | done |
| `nilweb.middleware.chain` | guards before a handler, finishers after | done |
| `nilweb.server.listener` | binding, accepting, request framing | done |

```sh
./scripts/serve.sh   # a real server on 127.0.0.1:8080
./scripts/test.sh    # 272 checks
./scripts/demo.sh    # the same requests, printed instead of served
```

All three take `NIL_SDK` if the framework is not a sibling checkout, and
`serve.sh` takes `NILWEB_PORT`.

It needs a framework with the socket, entropy and byte intrinsics — the commit
"Sockets, secure random, and byte access to text" or later.

## One connection at a time

There is a server, and it serves: bind, accept, read until the request is
framed, hand the text to the application, write the answer, close, repeat.

It handles one connection at a time, and will until the language grows
something to hand a connection to. There are no threads, no async, and no way
to wait on several sockets at once — that last one is the piece that would have
to arrive first. So this is a development server, a local tool, or something
behind a reverse proxy that absorbs the slow clients. Every accepted
connection gets a 15-second silence deadline, because a single-threaded loop
without one is a single slow client away from serving nobody.

No keep-alive: a connection is answered and closed. No TLS. A request is capped
at 1 MiB, header and body together, and the cap stops the reading rather than
rejecting afterwards.

### This used to say the opposite

The first version of this framework had no server and this section explained
why: the NIL runtime had no network intrinsics at all, `nil.std.net` was
protocol values with a note saying the transport was deliberately absent, and
the socket design under `nilstd/experimental` had nothing beneath it.

That was a gap in the framework rather than a fact to design around, so it was
filled in: `nil.std.net.socket` now exists, and so do
`nil.std.security.random` and `nil.std.text`'s `byte_at`/`from_byte`, which
closed the other two holes this framework had documented. See the framework's
[socket reference](https://github.com/exec-software/exec-nil-framework/blob/master/docs/standard-library/sockets.md).

## Differences from NyWeb

**Code-behind is gone.** NyWeb gave every page a class implementing `IPage` and
the router called it. Matrix has no capturing closures, and an array of class
handles compiles but does not execute, so a callable cannot be stored in a route
table. A route names its handler with text and the application switches on the
name. The cost is one `switch` per application; what it buys is a route table
that is plain data — readable, testable and reorderable without running it.

**The template engine is Matrix, not C++.** NyWeb's was 626 lines of C++
reached by FFI. This one is Matrix, because Matrix can do the string work
itself, and a template engine written in the language it serves is one the
people using it can read.

**Middleware is two lists, not a chain of wrapped calls.** The `next(request)`
shape every other framework uses needs somewhere to capture `next`, and Matrix
has no capturing closures. What replaces it is a list of guards that run before
a handler and may answer instead of it, and a list of finishers applied to
whatever response comes back. A guard cannot wrap a handler in an `attempt` or
carry a local across the handler; what it can do is be printed, which is more
than most middleware stacks manage. An unwired guard fails closed with a 500,
because a configuration mistake in a security check must not be a quiet pass.

**Everything is a value.** A response is a record; a finisher takes one and
gives back another. Nothing mutates a shared request object, which is what makes
a handler testable without a server.

**Parsing happens once.** NyWeb re-split the query string on every lookup, so a
page reading five parameters parsed the query five times. Here a request is
parsed at construction and read from arrays afterwards.

**It brought its own foundations.** NyWeb sat on `nyhttp`. There was no NIL
equivalent and no socket to write one over, so rather than shipping a native
shim inside this repository, the three things the framework was missing —
sockets, secure random values, and reading a byte of text as a number — were
added to the framework itself. Everything here is ordinary Matrix calling the
ordinary standard library.

## Sessions

`new-session-id` draws sixteen bytes from `nil.std.security.random` — kernel
entropy, no fallback to a weak source — and gives back thirty-two hex
characters. `begin-session` does that and puts the session in the store in one
step, which is what a sign-in handler wants.

It can fail, and says so rather than returning empty text. A machine whose
kernel will not provide entropy does not get a session; the answer there is a
500, not a guessable id. There is deliberately no fallback, because a
predictable session id is worse than no session: the failure is invisible until
someone is reading another user's pages.

This module used to refuse to generate an id at all, and shipped a
`development-id` labelled in capitals as not a credential, because nothing
cryptographically secure was reachable from Matrix. Both are gone — the
workaround with the gap that caused it.

The cookie is not configurable and that is the point: `HttpOnly` keeps the id
out of `document.cookie`, `SameSite=Lax` keeps it off cross-site POSTs,
`Path=/` scopes it, and `Max-Age` matches the store so the browser forgets when
the server does. `Secure` is the one choice left to the caller, because a
`Secure` cookie is not sent over the plain HTTP that development runs on.

## Templates read; they do not evaluate

`<%= user.name %>` does not mean "evaluate this expression". There is no
runtime evaluator in Matrix — no `eval`, no reflection over locals — so it
means "look this name up in the context the handler published". NyWeb's engine
parsed Ny expressions and the runtime evaluated them; that is not available and
would not be reconstructed here if it were.

It is a narrower contract and a sturdier one. A template can only read what a
handler decided to publish, so it cannot reach into a session, open a file or
call a service, and reviewing what a page can see is reading one paragraph.

## Percent-coding is byte-exact

`decode-component` is correct for all of Unicode: overlong encodings,
surrogate halves, truncated sequences and four-byte planes are all handled,
and ill-formed input becomes U+FFFD as the WHATWG standard requires.

`encode-component` is correct too, which it was not at first. Escaping a
non-ASCII byte means reading its value, and nothing reachable from Matrix
could do that: `nil.std.text` sliced by byte but had no byte-to-number
conversion, and `nil.std.core`'s `char_to_i32` takes a whole Unicode scalar
and *aborts the process* on a partial one — a fatal runtime error, not a fault
`attempt` can catch. The first version passed non-ASCII through unescaped and
said so here.

`nil.std.text` now has `byte_at` and `from_byte`, so this walks bytes and
escapes each one the grammar does not permit, which is what RFC 3986 actually
specifies: `café` encodes as `caf%C3%A9`, the euro sign as three escapes, an
emoji as four.

## Layout

```text
src/nilweb/text/scan.matrix          text splitting and folding
src/nilweb/http/method.matrix        request methods
src/nilweb/http/status.matrix        status codes and reason phrases
src/nilweb/http/headers.matrix       header fields
src/nilweb/http/uri.matrix           percent coding and query strings
src/nilweb/http/request.matrix       the inbound request
src/nilweb/http/response.matrix      the outbound response
src/nilweb/routing/router.matrix     route matching
src/nilweb/template/context.matrix   the data a template may read
src/nilweb/template/render.matrix    the .nilhtml engine
src/nilweb/session/session.matrix    sessions and their cookie
src/nilweb/static/files.matrix       serving a directory
src/nilweb/middleware/chain.matrix   guards and finishers
src/nilweb/server/listener.matrix    binding, accepting, request framing
src/main.matrix                      a demonstration, and the `answer` entry point
web/                                 the demonstration's templates and assets
tests/run-tests.matrix               every suite
tests/nilweb/tests/harness.matrix    the checks
```

A book lives at the path its name spells: `book nilweb.http.request` is
`src/nilweb/http/request.matrix`.

## License

MIT.
