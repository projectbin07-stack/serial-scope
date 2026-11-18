# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private reporting instead:
<https://github.com/projectbin07-stack/serial-scope/security/advisories/new>.

Include a description, an input that triggers the problem (a byte stream or `.sslog` file) and
the version you used. Reports are handled on a best-effort basis by a single maintainer.

## Scope

serial-scope reads untrusted bytes from serial ports, TCP sockets and log files. It never
executes received data, and it writes only to the paths given to it (`--log`, `--csv`,
`--snapshot-dir`, and files chosen in the GUI). The TCP transport is a plain, unauthenticated,
unencrypted client meant for a local network. Reports of interest include crashes, unbounded
memory use or hangs on crafted input (lines are capped at 4096 bytes and binary frames at 72
bytes between delimiters), and anything that makes the program write outside the requested
paths.
