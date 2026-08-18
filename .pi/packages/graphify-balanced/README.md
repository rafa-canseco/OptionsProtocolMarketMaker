# Graphify Balanced

Pinned hardened derivative of `@wwjd/pi-graphify@0.4.3` for passive context only.

Runtime allowlist:

- read an existing `graphify-out/graph.json` of at most 10 MiB;
- require Graphify's expected boolean/array schema;
- inject only array counts and boolean graph flags into Pi's system prompt.

Node labels, report text, and all other repository-controlled strings are excluded from the prompt. The extension registers no tools or commands and performs no subprocess, network, write, watcher, build, extraction, installation, or upgrade operation.

`provenance.json` pins the reviewed npm tarball and entrypoint hashes; `patches/passive-context-only.patch.gz` records the exact entrypoint derivation.

Run the local check with:

```bash
node --test .pi/packages/graphify-balanced/test/index.check.ts
```
