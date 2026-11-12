# Reporting security issues

Use the repository's enabled [private vulnerability reporting](https://github.com/exec-software/exec-nil-framework-web/security/advisories/new).
If that option is unavailable, open a minimal issue requesting a private
contact channel without including exploit details, credentials, or private
data. Ordinary bugs can be reported publicly.

Include the affected web and NIL Framework SDK revisions, platform, impact,
and a small reproduction using synthetic data. Remove credentials, session
identifiers, private paths, hostnames, and user content from examples and logs.
There is no guaranteed response-time SLA.

## Current scope

The [README](README.md#status) records the tested interpreter profile and known
gaps. Treat those limits as part of the current implementation; a passing test
suite is not a claim of production readiness or support for every execution
route and platform.

When reporting a problem, distinguish request parsing and framing, static-file
access, template rendering, session handling, and application-specific behavior.
Include the relevant configuration without private values. Exercise only local
instances or systems you own or have explicit permission to test, using bounded
requests that do not affect other users.
