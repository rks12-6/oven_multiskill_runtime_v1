# Project implementation rules

- Deployment targets are Linux only: Agilex Linux and 5090 Linux.
- Do not add Windows runtime, deployment, path, service, socket, or signal compatibility code.
- Prefer Linux primitives when they make safety explicit: Unix sockets, `fcntl` locks, POSIX signals,
  systemd, OpenSSH, file ownership, and mode bits.
- Windows may be used only as an editing/staging host; authoritative tests run on the two Linux targets.
- Do not import, execute, source, or dynamically load code from previous `skills/` or `labs/` projects.
- Every safety invariant in `docs/safety_invariants.md` requires an automated regression test.
- Do not add real-robot execution paths before fake-policy and fake-robot tests cover their failure behavior.

