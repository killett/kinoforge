# CLAUDE.md — kinoforge

## Session resume protocol (read this first, every session)
This project is built across multiple sessions, and a session can die mid-run — e.g. an API `400`
that poisons the conversation so every subsequent turn fails until it is cleared. On **every** new
or resumed session, before doing anything else:

1. Read `PROGRESS.md` at the repo root. It is the source of truth for where the build is.
2. Read the design doc and the implementation plan that `PROGRESS.md` points to.
3. Run `git log --oneline -20` to see what is already committed.
4. Resume from the first unchecked task in the plan. **Do not** redo work that is already committed.

If `PROGRESS.md` does not exist yet, you are at the very start of the project; create it as soon as
a design or plan exists (see Durability rules).

## Durability rules (always)
- **Git is the source of truth, not the conversation.** Commit after every completed task or
  passing test, with a clear message. Never end a step with completed work left uncommitted.
- **Keep `PROGRESS.md` current.** It must contain: the design-doc path, the plan path, the task
  checklist (each item done / in-progress / next), key decisions and gotchas, and the single next
  action. Update and commit it after each task.
- **Persist the brainstorm as it forms.** During brainstorming, append each validated design
  section to the design doc and commit it — never leave the agreed design only in the conversation.

## Process & testing
- **Superpowers owns the workflow:** brainstorm → plan → execute, with red/green TDD and two-stage
  review. Follow its skills for test structure, fixtures, mocking style, naming, and granularity —
  do not impose a competing test process.
- **Spec vs. this file:** the requirements (the *what*) live in the build brief you were handed.
  This file owns the *how* — process and durability. Where they overlap on process, defer to
  Superpowers; the durability rules above are additive.

<!-- Extend this file with your own project conventions (commands, style, gotchas) as you go. -->
