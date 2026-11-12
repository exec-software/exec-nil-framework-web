# Contributing to nil-web

nil-web is a server-side web framework written in Matrix. Start with the
[README](README.md) for its current capabilities, known gaps, and compatible NIL
Framework build. Keep new library behavior in Matrix where the language supports
it, and report any required framework changes separately.

## Development workflow

1. Use the SDK described in the README. Set `NIL_SDK` to its directory if it is
   not available through the sibling framework checkout.
2. Keep changes focused and add a regression case under `tests/` when changing
   behavior. Use synthetic requests and data.
3. Run the test suite from the repository root:

   ```sh
   ./scripts/test.sh
   ```

   This compiles the Matrix tests and runs them with `nilinterpreter`. Report
   the SDK revision, platform, and checks run; an interpreter pass does not
   establish native-route or cross-platform support.
4. Update the public documentation when setup, APIs, or observed behavior
   changes. Describe remaining gaps and any skipped or blocked checks in the
   pull request.

Submit changes through a pull request. The public branch requires an approving
review, resolution of review conversations, the GitHub Actions `secrets` check,
and the four `web-*` platform checks. Do not bypass those checks with a direct
push. The platform workflow builds the exact framework revision recorded in
[its configuration](.github/workflows/test.yml), then checks the unit suite,
real loopback HTTP behavior, filesystem boundaries and process cleanup.

For server changes, also exercise the affected HTTP behavior with bounded
clients against a local instance you own, then stop that instance. Keep
ordinary generated outputs under the ignored `build/` directory. The wire and
platform harnesses require a fresh external `--output` directory so their
fixtures and evidence cannot become tracked source.

## Public source review

Do not commit credentials, private configuration, internal notes, real user
requests or session data, developer-specific paths, or generated artifacts.
Use relative paths, explicit configuration, and synthetic examples. Review
history as well as the current files before publishing changes.

Run the path audit from the repository root after staging the intended files:

```sh
python3 -B tests/test_audit_paths.py
python3 -B scripts/audit-paths.py --root .
```

The path audit checks tracked text. The source-security workflow also scans
the working tree and fetched Git history with checksum-pinned
[Gitleaks 8.30.1](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1).
With that version installed, basic local scanner commands are:

```sh
gitleaks dir . --no-banner --redact
gitleaks git . --no-banner --redact
```

CI additionally fixes the default rule configuration, rejects inline scanner
suppressions, and bounds archive traversal, decoding and scan time.

Do not publish scanner reports or unreviewed logs: even redacted output can
expose private context. Scanning supplements source review; it does not replace
it. Report vulnerabilities through the process in [SECURITY.md](SECURITY.md).

Contributions to original project materials use the repository's
[MIT License](LICENSE). Preserve existing third-party notices.
