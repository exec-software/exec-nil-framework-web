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

The current server passes on Linux, Windows, Intel macOS and Apple Silicon
macOS: 1,128 unit checks, 38 real-wire checks, 15 static HTTP checks and 16
deadline-oracle controls on each host. The
[four-platform run](https://github.com/exec-software/exec-nil-framework-web/actions/runs/43162279298)
also verifies rooted-file behavior, unchanged inputs and owned-process cleanup.
These checks run through the NIL interpreter against a pinned framework source
build; they do not qualify a published SDK or native networking. All four
platform checks and the source-security audit are required for public changes.

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
| `nilweb.static.files` | rooted static reads, content validators and byte ranges | done within the profile below |
| `nilweb.middleware.chain` | guards before a handler, finishers after | done |
| `nilweb.http.framing` | bounded Content-Length/chunked frames and exact pipeline boundaries | done within the profile below |
| `nilweb.server.engine` | the event loop: accepting, framing, keep-alive | done within the profile below |
| `nilweb.time.clock` | HTTP dates, log stamps, ISO instants | done |
| `nilweb.log.access` | Combined Log Format access log, error log | done |
| `nilweb.config.settings` | a nildata config file, and virtual hosts | done |

```sh
./scripts/serve.sh   # a real server on 127.0.0.1:8080
./scripts/test.sh    # 1,128 checks
./scripts/demo.sh    # the same requests, printed instead of served
```

All three take `NIL_SDK` if the framework is not a sibling checkout, and
`serve.sh` takes `NILWEB_PORT`.

It needs a framework with `nil.std.io::read_rooted_snapshot` and
`nil.std.text::from_bytes`, as well as the socket and entropy APIs. Older SDKs
without these APIs cannot run this revision. The functional CI workflow pins the
exact compatible framework source. Build that revision with its
`scripts/build-sdk.sh` and point `NIL_SDK` at the resulting SDK. Web remains an
interpreter consumer: native
text does not yet provide the binary-string contract needed by this response
layer.

## Concurrency

An event loop, not a connection at a time. `poll` says which sockets can be
acted on; the engine frames whole requests out of whatever arrived and the
application answers them. Socket reads and writes are nonblocking, so a slow
peer does not make the loop wait for its next byte. Application handlers and
filesystem work still run synchronously on that loop.

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

These are historical measurements of the demonstration on one interpreter
build, not a throughput or latency guarantee for the current server.

**Keep-alive** is on by default for HTTP/1.1 and off for HTTP/1.0 unless the
client asks, which is the way round that avoids holding connections no client
will use again. A connection serves up to 1000 requests before being closed
anyway, so a long-lived client cannot pin a slot for the life of the process.

**Multiple server processes** can share a listening port on Linux and macOS
with `NILWEB_SHARE_PORT=1`, using `SO_REUSEPORT`. The Windows socket provider
does not support this option; the demonstration reports that sharing failed
and keeps its single listener. This server does not dispatch handlers across
threads or share in-memory sessions between processes.

**Three idle deadlines**, because the three cases differ: 10s for a connection
that has sent nothing (a scan, or a browser's speculative socket), 20s for a
request still arriving (which is what slowloris looks like), and 30s between
keep-alive requests (a real client thinking). Each wire request, including its
headers, is capped at 1 MiB; oversized declarations are rejected before waiting
for the body. One additional socket read of up to 64 KiB can hold bounded
pipeline surplus. The default connection limit is 1024, configurable through
`connectionLimit`; accepted connections above that limit are closed.

### What is still missing for parity

No TLS — use a separate TLS terminator when serving HTTPS. No HTTP/2, no
response compression (the framework's compression prototype is outside the
supported standard library), no
`sendfile`, no reverse proxying. `poll` is O(n) per call in the watch set, which
starts to matter in the thousands; the interface is shaped so `epoll` can
replace it underneath without callers noticing.

The current HTTP/1 profile is explicit:

- Framing requires CRLF. Identical repeated `Content-Length` values are accepted;
  malformed or conflicting lengths, and length plus transfer encoding, are
  rejected with a final response and connection closure. Chunked bodies are
  decoded within the request cap. Extensions and trailers are validated;
  trailers are discarded. Other transfer codings receive an explicit rejection.
- Pipelined requests retain exact message boundaries and surplus bytes across
  response writes. Partial requests keep a deadline that incoming fragments
  cannot extend. Half-closing input still permits complete requests to finish.
- HTTP/1.1 `Expect` requests receive `417` immediately; interim `100 Continue`
  negotiation is not implemented. Clients may retry without the expectation.
  HTTP/1.0 `100-continue` is ignored as required by HTTP semantics.
- Preconditions run before ranges. `HEAD` ignores `Range`, keeps the full
  representation length and sends no body. Strong content-based `If-Range` tags
  are supported; date-form `If-Range` conservatively sends the full response.
  Invalid date conditions are ignored; parsing accepts canonical IMF-fixdate.
- Static reads reject every symlink or Windows reparse point below the trusted
  configured root, including intermediate directories. One file handle supplies
  bounded bytes and metadata; SHA-256 identifies those exact bytes. This does
  not protect against privileged mount changes or hard links placed in the
  public directory. Keep that directory under the application's control.

The static limit is 2 MiB. The runtime computes the content hash and copies binary
bytes into interpreter text in bulk. A local loopback check served and verified a
1 MiB response in about 10 ms; this is an observed test result, not a throughput
guarantee. The pure Matrix SHA-256 module remains a tested reference implementation.

`tests/test_static_snapshot.py` verifies binary bodies, conditional/range/HEAD
responses, preserved-timestamp edits, filesystem links and the size cap.
`scripts/test-http-wire.py` checks real framing, backpressure, pipelines,
half-close and deadlines. Both use bounded, owned loopback servers and verify
cleanup; `scripts/test-platform.py` runs the complete platform campaign.

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

**An unknown schema is not applied.** The demonstration reports the rejected
configuration and uses defaults, then applies environment overrides. It does
not abort startup solely because the configuration file was rejected.

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

**Buffered, and flushed when the server is idle.** The logger batches lines
before calling `nil.std.io::append_text`, rather than opening the log on every
request. It flushes on an idle pass or when the ordinary access/error buffer
reaches 64 lines. Lines buffered when the process dies are lost; `flush-now`
allows an explicit flush. Failed writes discard the affected batch and increase
`dropped-count` instead of retrying an ever-growing buffer.

**Log fields are sanitized.** Quotes, backslashes, control bytes and DEL are
replaced with spaces, then surrounding whitespace is trimmed. This prevents
those bytes from injecting extra lines or breaking quoted fields; it does not
preserve the original field length. A malformed request logs a dash in place
of its request line.

**Historical logging measurements** showed 366 req/s becoming 291, before the
current content hashing and framing changes.
One cause was found and fixed: the loop was parsing every request twice, once
for the log and once inside `answer`, which was worth 270 → 309 on its own.
The remaining fifth is not accounted for. Two likely-looking fixes — a
clean-path scan instead of a byte-by-byte rebuild, and caching the formatted
timestamp per second — both measured at no change, so they are kept on their
merits for long inputs rather than credited with a gain they did not produce.

## Static files

`serve-static` reads a bounded snapshot through `nil.std.io::read_rooted_snapshot`.
That snapshot supplies the bytes, modification time and SHA-256 digest from one
opened file. The 2 MiB cap is checked before the content allocation; oversized
files receive 413. Other read refusals and missing files receive 404.

**Content ETags.** The strong tag is the quoted lowercase SHA-256 of the exact
returned bytes, including binary content. It changes when content changes even
if the file's size and timestamp are preserved. The runtime computes the digest;
the pure Matrix SHA-256 module is a tested reference, not the static-serving hot
path. Revalidation still reads and hashes the bounded file before deciding
whether a body is needed; this module does not cache snapshots between requests.

**Preconditions.** `If-Match` uses strong comparison and takes precedence over
`If-Unmodified-Since`. `If-None-Match` uses weak comparison and suppresses
`If-Modified-Since` whenever the tag field is present. A matching
`If-None-Match` produces 304 for GET/HEAD and 412 for other applicable methods.
Failed `If-Match` or `If-Unmodified-Since` conditions produce 412. The 304 response
carries validators and cache policy, with no body, `Content-Length` or
`Content-Type`.

**Date validators.** `Last-Modified` and date conditions use second resolution.
Only canonical IMF-fixdate is parsed; malformed, obsolete or otherwise
unsupported date values are ignored. A date alone cannot distinguish two
different representations written within the same second; content ETags do.
Ignoring a date does not bypass other preconditions or range handling.

**Byte ranges.** `bytes=0-9`, `bytes=500-`, `bytes=-500` for the last 500. A
206 carries `Content-Range`; a first byte past the end is a 416 naming the real
length. Only GET applies ranges, after preconditions. HEAD keeps the full
representation length and sends no body. A matching single strong `If-Range`
tag permits the range; weak tags, mismatches and date-form `If-Range` receive
the whole representation. Multi-range requests and unknown units are ignored;
no multipart response is produced. Empty files also ignore `Range`.

**Path and file boundaries.** After mapping the URL to a relative name, path
checks reject traversal components, drive syntax, backslashes, doubled
separators, NUL and dotfiles. The framework then opens descendants relative to
the trusted root, rejecting symlinks and Windows reparse points in intermediate
and final components and accepting only regular files. This is not protection
against privileged mount changes or hard links placed inside the public root.
Unknown extensions use `application/octet-stream`.

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

Serving freezes the cache after each template's first load, so subsequent
cache hits do not recheck timestamps or reread that template. Set
`NILWEB_RELOAD=1` to recheck timestamps while editing; edits that preserve the
timestamp can remain cached. This template cache is separate from static-file
content hashing. Template files are trusted application inputs and do not use
the rooted static-file snapshot boundary.

## Differences from NyWeb

**Routes name their handlers.** NyWeb used page classes implementing `IPage`.
This framework keeps routes as data: each route names a handler and the
application dispatches on that name. It does not require a stored callback or
capturing closure. This describes the server's design, not a claim that all
class arrays or function values are unsupported by the language.

**The template engine is Matrix, not C++.** NyWeb's was 626 lines of C++
reached by FFI. This one is Matrix, because Matrix can do the string work
itself, and a template engine written in the language it serves is one the
people using it can read.

**Middleware uses two lists.** Guards run before the handler and may answer
instead; finishers apply to the resulting response. This API does not wrap a
handler in a captured `next` callback or carry a guard's local state through
that callback. An unwired guard produces 500 instead of silently proceeding.

**Handlers can be tested without a socket.** Requests and responses are records,
and finishers return a response. This does not promise immutable records or
deep-copy semantics; the server engine also uses mutable records internally.

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

`<%= user.name %>` looks up a name in the context the handler published. The
template engine does not evaluate arbitrary Matrix expressions or reflect over
the handler's locals. Includes and layouts use trusted template files; template
values do not gain access to arbitrary application operations.

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
src/nilweb/http/framing.matrix       bounded wire framing and pipeline boundaries
src/nilweb/routing/router.matrix     route matching
src/nilweb/template/context.matrix   the data a template may read
src/nilweb/template/render.matrix    the .nilhtml engine
src/nilweb/session/session.matrix    sessions and their cookie
src/nilweb/static/files.matrix       serving a directory
src/nilweb/crypto/sha256.matrix      reference SHA-256 implementation and tests
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
