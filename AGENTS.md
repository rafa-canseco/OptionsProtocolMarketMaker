# Market Maker Instance

This file is the canonical protocol for this market-maker checkout. When the local
context router discovers an options workspace, the workspace orchestrator protocol
at the absolute path printed by that router also applies.

## Scope

- Write only inside `marketMaker/` and the assigned worktree.
- Read sibling repositories only for an explicitly declared interface dependency.
- Never access, search, glob, or inspect any external repository or directory whose
  basename is `options-scenarios`, regardless of its location relative to this
  checkout.
- Preserve unrelated dirty-worktree changes. After full verification and independent
  review pass, create a local ticket-scoped commit, push its feature branch, open a
  draft PR to `staging`, attach evidence plus `Fixes B1N-<id>`, and move Linear to
  Review by default. Do not merge or deploy unless the user requests it. After
  merge, let the GitHub–Linear integration move the linked issue to Done
  automatically.

## Active Context

- Default scope is v2: Base-only, vault-first, ETH/USDC first.
- Run `./scripts/harness-context.sh` before planning. It detects both a normal
  workspace checkout and a checkout below `.worktrees/`, validates the routed
  workspace files, and prints their resolved paths. In a standalone clone it
  reports that workspace-only context is unavailable and this local protocol
  remains authoritative.
- When workspace context is available, read the files routed by
  `harness/bin/context marketMaker` at the absolute paths printed by the local
  router. The routed repository protocol is always resolved to this checkout's
  `AGENTS.md`, never another market-maker checkout.
- The market maker is also the initial allocator, but strategy and asset boundaries
  must remain modular for later curated vaults.
- Do not load v1, Solana, Arc, XLayer, hackathon, or broad legacy playbook context
  unless the Linear ticket explicitly targets legacy work.

## Tooling

- Python 3.13 target (project compatibility is Python 3.12+).
- Package manager: `uv`, never pip or poetry.
- Format/lint: `uv run --frozen ruff format --check` and
  `uv run --frozen ruff check`.
- Tests: `uv run --frozen pytest`.
- Context routing: `./scripts/harness-context.sh`.
- Canonical verification: `./scripts/harness-check.sh doctor|fast|full`.

## Start Protocol

1. Run `./scripts/harness-context.sh`. When operating in a workspace, run the
   depth-independent command
   `workspace_root="$(./scripts/harness-context.sh --workspace-root)" && "$workspace_root/harness/bin/doctor"`.
2. Read the assigned Linear issue and acceptance criteria.
3. Post a plan and wait for approval unless the user's direct instruction already
   approved that plan.
4. When workspace context is available, resolve `workspace_root` with the command
   above and create/resume `$workspace_root/harness/runs/<ISSUE-ID>/`; record the
   absolute worktree and ownership there.
5. Start v2 work from `staging` in a feature branch/worktree.

## Implementation And Review

- One logical scope per ticket and one agent owner per worktree.
- Record interface decisions that backend or contracts consume in the ticket run's
  `decisions.md`.
- Keep private keys, credentials, live transaction material, and raw operational
  logs out of agent memory and Git.
- An independent reviewer verifies pricing/hedging/allocator invariants and the
  acceptance criteria. The implementer does not approve its own work.
- Do not mark work done while the canonical check is red; report existing failures
  honestly in `verification.json`.

## Git And Linear

- V2 branches and PRs target `staging`, never historical `dev`.
- Use conventional commits, include the Linear ID, and stage only files owned by the
  ticket. Never commit a failing or incomplete handoff.
- Run `./scripts/harness-sensitive-check.sh` before any commit or push. It checks
  exact index content and tracked working-tree edits separately and reports paths
  only.
- Move Linear to Review only after implementation, independent review, and full
  verification evidence are complete.
