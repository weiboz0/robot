---
name: autopilot-plan
description: >-
  Drive a plan through the full rover-chatbot dev workflow end to end: write/locate
  the plan, run the 2-model plan-review gate, implement on a local branch, write
  tests, run the 3-way code-review gate, run ci-local.sh, open a PR, optionally
  auto-merge, record both review rounds back into the plan, and write a
  post-execution report. Use when the user says "autopilot", "run the plan", or
  asks to take a task through plan→review→implement→test→review→PR automatically.
---

# Autopilot a plan through the dev workflow

This skill executes the project's standing workflow (CLAUDE.md + the
`dev-workflow-gate` memory) autonomously for one plan. It is the automated form
of that workflow — it does NOT replace or contradict it. Apply it to any
non-trivial file-changing task.

## Inputs / flags (read from the user's request; pick sane defaults)

- `AUTO_MERGE` — default **false**. Merge to `main` only if the user explicitly
  set this true for this run. This is the explicit opt-in that satisfies
  CLAUDE.md's "commit and push only when asked".
- Plan reference — an existing `docs/plans/NNN - *.md`, or a task to plan from
  scratch.

## Ground rules (must stay consistent with CLAUDE.md + memory)

- **Local branch, never a worktree** (`git checkout -b <topic>` in the project
  dir). [[dev-workflow-gate]]
- **Plans live in `docs/plans/`** with a numbered prefix, **contiguous** (no
  gaps — renumber if a plan is abandoned). One logical change per branch/PR.
- **Reviewer commands** (exact):
  - Opus: spawn a subagent with the Agent tool, `model: opus`.
  - codex (GPT-5.5): `codex exec --skip-git-repo-check "<prompt>"`
  - glm-5.1: `~/.opencode/bin/opencode run -m opencode-go/glm-5.1 "<prompt>"`
    ([[opencode-binary-path]] — not on PATH).
- **Never commit secrets**; `.env` is gitignored.
- Gather each reviewer's verdict as BLOCKING / NON-BLOCKING; resolve all
  BLOCKING before advancing a gate.

## Steps

1. **Plan.** Write or update `docs/plans/NNN - <title>.md` (next contiguous
   number). Include goal, design, deliverables, testing, risks, and a `## Stages`
   list. Append two empty sections to fill in later: `## Reviews` and
   `## Post-execution report`.

2. **Plan-review gate (2 models).** Send the plan to **Opus** and **codex** in
   parallel. Resolve every BLOCKING finding by editing the plan. Re-verify if a
   reviewer requested changes. **Record** a short summary of each reviewer's
   verdict + how blockers were resolved into the plan's `## Reviews` section
   (subsection "Plan review").

3. **Implementation.** Create/switch to the local feature branch. Write the code
   to match the (revised) plan. Keep changes scoped to this one plan.

4. **Testing.** Write/extend test cases covering the change (only if the change
   needs them — trivial doc edits may not). Make them runnable with no hardware
   where possible (fake interfaces).

5. **Code-review gate (3-way).** Send the staged diff to **Opus + codex +
   glm-5.1** in parallel. Resolve every BLOCKING finding, then **re-verify the
   fixes** with the reviewers that raised them. **Record** the verdicts + blocker
   resolutions into `## Reviews` (subsection "Code review").

6. **CI gate.** Run `./ci-local.sh` (unit + integration tests). It must exit 0.
   If it fails, fix and re-run before proceeding — do not open a PR on red CI.

7. **PR.** Commit (message per CLAUDE.md, ending with the `Co-Authored-By: Claude
   Fable 5` trailer). Push the branch and open a PR with `gh pr create` (body
   ends with the Generated-with-Claude-Code line). If `gh` is unauthenticated,
   stop and give the user the compare URL + `gh auth login` instruction.

8. **Merge.** If `AUTO_MERGE` is true AND the code-review gate passed AND CI is
   green, merge the PR (`gh pr merge --squash`). Otherwise leave it open for a
   human and say so.

9. **Record reviews back to the plan.** Ensure `## Reviews` holds both rounds
   (already written in steps 2 and 5); commit the updated plan.

10. **Post-execution report.** Append to the plan's `## Post-execution report`:
    what was actually implemented, what deviated from the plan and why, the key
    tradeoffs, what was deferred/out-of-scope, and the CI/review outcomes. Keep
    it short and honest (state anything skipped or failed).

## Notes

- Pure analysis/read-only tasks are exempt from this skill (no files change).
- Scale review depth to the task: small change → lighter prompts; risky/safety-
  critical change (e.g. anything that physically moves the rover) → adversarial
  verification and a re-review of every fix.
