# nil-web

A server-side web framework for the [Matrix](https://github.com/exec-software/exec-nil-framework)
language, written in Matrix.

It is a rewrite of NyWeb, an ASPX-inspired framework written in Ny against a
much older NIL framework. The design survived the move; almost none of the code
did, because the languages differ in ways that reach the architecture. See
[Differences from NyWeb](#differences-from-nyweb).

[Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

[![Support us on Ko-fi](https://img.shields.io/badge/Support_us-Ko--fi-FF5E5B?logo=kofi&logoColor=white)](https://ko-fi.com/execsoftware)

## Status

All 764 current checks pass through the NIL interpreter on Linux with framework
SDK `9fa354d`. This includes content-based validators and HTTP precondition/range
ordering. The server supports concurrent connections, configuration, virtual
hosts, and access and error logs. Request framing, filesystem containment and
large-file hashing still need the work listed below.

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
| `nilweb.static.files` | static files, validators and byte ranges | partial; see remaining work |
| `nilweb.middleware.chain` | guards before a handler, finishers after | done |
| `nilweb.server.engine` | the event loop: accepting, framing, keep-alive | partial; see remaining work |
| `nilweb.time.clock` | HTTP dates, log stamps, ISO instants | done |
| `nilweb.log.access` | Combined Log Format access log, error log | done |
| `nilweb.config.settings` | a nildata config file, and virtual hosts | done |

```sh
./scripts/serve.sh   # a real server on 127.0.0.1:8080
./scripts/test.sh    # 764 checks
./scripts/demo.sh    # the same requests, printed instead of served
```

All three take `NIL_SDK` if the framework is not a sibling checkout, and
`serve.sh` takes `NILWEB_PORT`.

It needs a framework with the socket, entropy and byte intrinsics — the commit
"Sockets, secure random, and byte access to text" or later.

## Concurrency

An event loop, not a connection at a time. `poll` says which sockets can be
acted on; the engine frames whole requests out of whatever arrived and the
application answers them. Many connections are open at once and none can hold
up another by sending slowly, reading slowly, or stopping halfway.

Historical measurements before content hashing and framing hardening, on the
demonstration application: a client that stalls for 1.5
seconds in the middle of its request headers does not delay 120 requests on 40
other connections, which finish in 0.72s. 64 connections each sending a
two-part request with a quarter-second pause are served in about one pause
rather than 64 of them.

Throughput, 32 connections of 40 keep-alive requests each: **366 req/s**, zero
errors. Per route, at 16 connections:

| req/s | route | what it does |
| --- | --- | --- |
| 422 | `/search` | parse, route, plain text |
| 403 | `/hello/ada` | parse, route, escape a capture |
| 317 | `/` | template with a layout and a partial |
| 274 | `/posts` | the same with a loop |
| 243 | `/assets/app.css` | a file, with validators |

With logging on, each of those drops by roughly a fifth; see Logs below.

That is a tree-walking interpreter rendering templates, so it is not Apache's
number. It is the shape that matters: adding connections does not add latency
until the single core is saturated.

**Keep-alive** is on by default for HTTP/1.1 and off for HTTP/1.0 unless the
client asks, which is the way round that avoids holding connections no client
will use again. A connection serves up to 1000 requests before being closed
anyway, so a long-lived client cannot pin a slot for the life of the process.

**More than one core** means more than one process, because the interpreter
holds one stack, one heap and one set of globals with no lock anywhere near
them. `NILWEB_SHARE_PORT=1` turns on `SO_REUSEPORT` so several copies can share
a port and the kernel balances accepts between them. That is nginx's shape
rather than Apache's prefork, and it is the one this runtime can keep.

**Three idle deadlines**, because the three cases differ: 10s for a connection
that has sent nothing (a scan, or a browser's speculative socket), 20s for a
request still arriving (which is what slowloris looks like), and 30s between
keep-alive requests (a real client thinking). Requests are capped at 1 MiB and
the cap stops the reading rather than rejecting afterwards. At most 1024
connections are open at once; past that, new ones are closed immediately
rather than queued, because a queue there is memory held for a client already
being refused.

### What is still missing for parity

No TLS — it wants an intrinsic family over a TLS library, and until then this
belongs behind something that terminates it. No HTTP/2, no compression (the
standard library's `gzip_compress` is still an unbacked declaration), no
`sendfile`, no reverse proxying. `poll` is O(n) per call in the watch set, which
starts to matter in the thousands; the interface is shaped so `epoll` can
replace it underneath without callers noticing.

Current code review also identifies these remaining boundaries:

- **Request framing:** malformed `Content-Length` can be treated as a zero-length
  body, and `Transfer-Encoding` is not validated. Invalid framing needs an
  explicit rejection and connection closure.
- **Pipelining:** requests received together are not separated reliably; clearing
  the input after a response can discard the next request.
- **Static hashing throughput:** validators now use SHA-256 of the bytes served.
  The pure Matrix implementation passes known vectors and binary reference
  checks, but takes about 12 seconds for 1 MiB in the interpreter. It needs a
  faster runtime path before serving large public assets.
- **Static-file containment:** textual traversal checks do not keep filesystem
  links inside the public root. Reads and metadata need a shared, root-constrained
  filesystem boundary, including intermediate symlinks and Windows junctions.

Conditional requests now run before ranges. `HEAD` ignores `Range` and retains
the full representation length while suppressing the wire body. Strong `If-Range`
tags are supported; date-form `If-Range` conservatively sends the full response.
Invalid date conditions are ignored; date parsing currently accepts canonical
IMF-fixdate only. The new checks include same-size, same-timestamp binary edits.

The 764-check run is an interpreter suite. Socket-level framing validation and
physical filesystem containment remain separate work.

### This used to say the opposite

The first version of this framework had no server and this section explained
why: the NIL runtime had no network intrinsics at all. The second had one that
served a single connection at a time, because there were no threads and no way
to wait on several sockets.

Both were gaps in the framework rather than facts to design around, so they
were filled in: `nil.std.net.socket` with `poll`, non-blocking transfer and
`SO_REUSEPORT`; `nil.std.security.random`; `nil.std.text`'s `byte_at` and
`from_byte`; and `nil.std.io`'s `modified_at`. See the framework's
[socket reference](https://github.com/exec-software/exec-nil-framework/blob/master/docs/standard-library/sockets.md).

## Configuration and virtual hosts

A `nil.web.nildata` file, overridden by the environment. nildata rather than
JSON or a format of our own, because it is the framework's own:
`nil.project.nildata` describes a build, so `nil.web.nildata` describing a
server needs no new parser, no new tooling and nothing to learn.

```
nildata 1
object
  schema is "nil.web.v1"
  host is "127.0.0.1"
  port is 8080
  connectionLimit is 1024
  accessLog is ""
  sites is an array
    item is an object
      host is "one.example"
      templateRoot is "sites/one"
      publicRoot is "sites/one/public"
    end object
    item is an object
      templateRoot is "web"
    end object
  end array
end object
```

The file is the record of intent and belongs in version control; the
environment overrides it, because a container or a test run needs to change one
value without editing a file it does not own. `NILWEB_CONFIG` names the file,
and `NILWEB_HOST`, `NILWEB_PORT`, `NILWEB_ACCESS_LOG`, `NILWEB_ERROR_LOG`,
`NILWEB_SHARE_PORT` and `NILWEB_RELOAD` each override one value.

**Everything has a default and a missing file is not an error** — a server that
will not start without a config file is one nobody can try out. A file that is
*there* and will not parse is a different matter: it is said plainly and the
defaults are used, because starting while silently ignoring a file somebody
wrote is worse than either refusing or obeying.

**An unknown schema is refused rather than read hopefully.** A file declaring a
version this does not know may mean something different by the same key names,
and guessing is how a server ends up listening somewhere nobody asked for.

**Sites are matched on `Host`**, with the port stripped and the case folded,
each with its own template and public roots. A site with no `host` is the
default; **without one, an unknown `Host` is a 404 rather than whichever site
happens to be first** — which is how one tenant ends up reading another's
pages. There is deliberately no wildcard matching: `*.example.com` looks
helpful and is the shape that serves the wrong tenant when a subdomain is added
and nobody re-reads the config.

## Logs

The Combined Log Format, which is Apache's and nginx's, so anything that reads
a web server's log already reads this one:

```
127.0.0.1:54321 - - [06/Nov/1994:08:49:37 +0000] "GET /posts?page=2 HTTP/1.1" 200 1234 "http://example.com/from" "curl/8.5.0"
```

`NILWEB_ACCESS_LOG` and `NILWEB_ERROR_LOG` turn them on. Off by default,
because a framework that starts writing files into whatever directory it was
launched from fills a disk on someone's laptop.

**Buffered, and flushed when the server is idle.** `nil.std.io`'s only
appending call opens, writes and closes, which is three syscalls per request on
the hot path, and the handle API that would avoid them is private and takes raw
byte buffers a Matrix page cannot build. So lines are held and written in
batches — on an idle pass, which the loop already detects, or when 64 have
accumulated. The cost is a window: lines buffered when the process dies are
lost. That is the trade the available I/O imposes, and `flush-now` is there for
anything that would rather pay the syscalls.

**A log line cannot be forged.** Header values are attacker-controlled, and a
newline in one would end the line and start another of the attacker's
choosing — a fabricated entry in a file someone will later read as evidence. A
quote would close a field early and do the same to a parser; a control byte
would move a terminal's cursor or set a colour. All are replaced with a space,
one space per byte so the length a reader sees is not changed by sanitising. A
malformed request logs a dash where its request line would be, rather than its
bytes.

**Logging costs about a fifth of the request rate** — 366 req/s becomes 291.
One cause was found and fixed: the loop was parsing every request twice, once
for the log and once inside `answer`, which was worth 270 → 309 on its own.
The remaining fifth is not accounted for. Two likely-looking fixes — a
clean-path scan instead of a byte-by-byte rebuild, and caching the formatted
timestamp per second — both measured at no change, so they are kept on their
merits for long inputs rather than credited with a gain they did not produce.

## Static files

Served with the three things a static file server is expected to do about not
re-sending what the client already has.

**`Last-Modified` and `If-Modified-Since`**, compared at second resolution,
because an HTTP date has no finer one — a file written 300ms after the second
the client was told would otherwise look modified on every request forever. An
unreadable date counts as modified and gets the full representation, which is
always correct; reading a date wrongly and answering 304 serves a stale page,
which is the failure that matters.

**ETag and `If-None-Match`.** The tag is built from size and modification time
— what Apache does by default, and sound for the same reason: a file whose
length and mtime are both unchanged is, barring deliberate effort, unchanged.
A content hash would be stronger and wants a hash function the standard
library does not have yet. A client holding the current tag gets a 304 with no
body, no `Content-Length` and no `Content-Type`, because the response is not
the representation.

**Byte ranges.** `bytes=0-9`, `bytes=500-`, `bytes=-500` for the last 500. A
206 carries `Content-Range`; a first byte past the end is a 416 naming the real
length, so a client resuming a download discovers the file changed size rather
than being quietly handed the wrong bytes. One range per request — a
multi-range request gets the whole file, which RFC 9110 permits and which
avoids a multipart body for a case almost nothing sends.

**Refusal by shape, before touching the disk** — traversal in any spelling,
absolute names, drive letters, backslashes, doubled separators, null bytes and
dotfiles. An unknown extension is `application/octet-stream` and never
`text/html`, because a browser told something is HTML will run the script in it.

The dates come from `nilweb.time.clock`, which turns epoch milliseconds into
HTTP's date format, the Common Log Format stamp and an ISO instant. The
calendar arithmetic is Hinnant's `civil_from_days`, written out rather than
approximated, and its 260 test expectations were generated from a different
calendar implementation over leap days, century non-leap years and forty random
instants — calendar code that is wrong one day in four years looks right in
every test written in the other three.

## Templates are cached

A page used to mean reading three files and tokenising all of them on every
request, and that alone halved throughput against a route returning plain
text. Templates are now parsed once and kept, keyed by path and validated
against the file's modification time — which is why `nil.std.io` grew
`modified_at`: without it a cache cannot tell whether what it holds is current
except by re-reading the file, which is the thing the cache exists to avoid.

Serving freezes the cache, so a page costs no filesystem call at all. Set
`NILWEB_RELOAD=1` to leave it thawed while editing templates.

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
src/nilweb/server/engine.matrix      the event loop
src/nilweb/time/clock.matrix         HTTP dates, log stamps, ISO instants
src/nilweb/log/access.matrix         access and error logs
src/nilweb/config/settings.matrix    the config file and virtual hosts
nil.web.nildata                      the example configuration
src/main.matrix                      a demonstration, and the `answer` entry point
web/                                 the demonstration's templates and assets
tests/run-tests.matrix               every suite
tests/nilweb/tests/harness.matrix    the checks
```

A book lives at the path its name spells: `book nilweb.http.request` is
`src/nilweb/http/request.matrix`.

## Support our work

You can support Execute Software's work through
[Ko-fi](https://ko-fi.com/execsoftware). Donations are optional and help fund
continued development; they do not change what the project provides or what
its license permits.

## License

nil-web is licensed under the [MIT License](LICENSE).
