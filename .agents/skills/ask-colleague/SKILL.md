---
name: ask-colleague
description: >
  Ask the repository's Colleague harness for an independent read-only explore
  or review, a previewed scoped write, or feedback on a finished work item. Use
  when the user asks to consult a colleague or when a fresh, independently
  verifiable perspective would materially improve a non-trivial repo task.
---

# Ask Colleague for an independent perspective

Read
[`../../../.claude/skills/ask-colleague/SKILL.md`](../../../.claude/skills/ask-colleague/SKILL.md)
completely before taking task actions, then follow it as the canonical operating
procedure.

Use the existing portable resolver at
`../../../.claude/skills/ask-colleague/scripts/ask-colleague.sh`. Treat its
output as a second opinion to verify, never as authority. Read-only `explore`
and `review` are safe defaults; obtain user authorization before any
side-effecting `write --apply` or `write --pr` action.
