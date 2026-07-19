---
name: assign-to-workforce
description: >
  Fan out a converged Devague plan's dependency waves to isolated workers with
  human approval of the implementation split and test-gated reconciliation.
  Use when the user asks to assign a plan to a workforce, fan out work, or run
  approved plan waves in parallel.
---

# Assign a plan to the workforce

Read
[`../../../.claude/skills/assign-to-workforce/SKILL.md`](../../../.claude/skills/assign-to-workforce/SKILL.md)
completely before taking task actions, then follow it as the canonical operating
procedure.

Use the existing portable resolver at
`../../../.claude/skills/assign-to-workforce/scripts/assign-to-workforce.sh`.
Do not dispatch workers before the user approves the implementation split, and
do not let a worker mutate Devague plan state.
