# Graphify Automatic

Pinned hardened derivative of `@wwjd/pi-graphify@0.4.3` that lets Pi use a local AST graph without user-written Graphify commands.

Runtime behavior:

- injects a bounded graph-presence summary;
- registers exactly one read-only `graphify_lookup` tool that Pi is instructed to call automatically for architecture, dependency, caller, flow, path, module, and impact questions;
- computes a debounced SHA-256 content digest over relevant tracked and untracked source files before lookup;
- performs one serialized `graphify update .` only when the digest changed or the graph is unavailable;
- resolves the audited `graphifyy==0.9.46` uv-tool executable by absolute path outside the repository and runs it with an empty environment, isolated HOME, timeout, and no inherited PATH or provider credentials;
- returns only bounded identifiers, code paths, source locations, and allowlisted relations that matched strict grammars;
- falls back to Pi's normal search/read tools without blocking or asking the user to run commands.

It has no permanent watcher, semantic extraction, upgrade, auto-install, MCP, global settings, network integration, report injection, or committed graph artifacts.

`provenance.json` pins the reviewed npm tarball and entrypoint hashes. The deterministic gzip patch records the exact derivation.

```bash
node --test .pi/packages/graphify-balanced/test/index.check.ts
```
