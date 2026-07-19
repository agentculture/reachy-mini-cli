---
name: spec-to-plan
description: >
  Turn a converged Devague spec into a buildable, dependency-ordered plan with
  complete claim coverage and testable acceptance criteria. Use after think and
  challenge, or when the user asks to plan a spec or create an implementation
  plan.
---

# Turn a spec into a plan

Read
[`../../../.claude/skills/spec-to-plan/SKILL.md`](../../../.claude/skills/spec-to-plan/SKILL.md)
completely before taking task actions, then follow it as the canonical operating
procedure.

Use the existing portable resolver at
`../../../.claude/skills/spec-to-plan/scripts/spec-to-plan.sh`, or the installed
`devague plan` CLI. Never edit `.devague/` state by hand, and never confirm an
agent-origin task on the user's behalf.
