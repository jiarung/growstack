# Contributing

This repo is maintained by one person and one or more AI agents, and most of
its history is the two of them correcting each other. The rules below are the
ones that came out of that. They apply to agents and humans alike.

## Commit, then review, then push — in that order

The commit message is the review artifact. It is written so that a reviewer
who was not in the room can judge the change from the message and the diff
alone: what changed, why, what was verified and how, and what a previous
review found. A message like that is wasted if the commit is already on
`origin/main` when the reviewer reads it — at that point review is
after-the-fact approval, and the pressure to rationalise what shipped is
real.

So:

1. **Commit locally** with the full message. Not a placeholder; the message
   you would want to be judged on.
2. **Ask for review** before pushing. Two reviewers are available:
   - **codex**, adversarially: `codex exec --sandbox read-only 'Adversarial
     review of commit <hash> (git show <hash>). …'` — name the files, say what
     the change claims, list the specific things to hunt for, ask for
     file:line. Ask for defects, not approval; tell it to say "fine" for what
     it checked and found fine, so silence is not ambiguous.
   - **the maintainer**, who reads the message and the diff.
3. **Fix what the review finds** as a follow-up commit (or amend/fixup if
   nothing has been pushed), and say in its message what the review found
   and what was done — "Codex, on `<hash>`: …" is the convention. A finding
   that turns out to be wrong is also recorded, with why.
4. **Push when told to.** "commit and push" means both; "commit" means the
   first only. An agent that is unsure asks.

A review that comes back clean is still recorded in the next message that
touches the area ("Everything else in the review came back fine: …"), so
the next reader knows what was already checked.

## The message itself

Subject line in the imperative, lower-case prefix (`feat:`, `fix:`, `docs:`,
`test:`), and specific enough to be the one-line changelog. The body says
why, not what — the diff says what. It names what was verified ("exercised
live: …", "measured against the running stack: …") and distinguishes that
from what was reasoned about. If something was tried and abandoned, it says
so, because the next person will try it too.

## Verification is part of the change

A claim about behaviour is tested against the thing itself, not derived from
documentation or from what the code appears to do. This repo has an
incident log (`docs/incidents/`) with several entries that begin "the audit
said X; the audit was wrong". When a test cannot be run here (firmware,
another host), the message says so and the review is asked to compensate.

## Where things are written down

- Operational procedures, failure classes, redeploy steps: `broker/MAINTENANCE.md`
- Every failure, dated, with its evidence: `docs/incidents/`
- Ways a query here has returned a confident wrong answer: `broker/QUERYING.md`
- The one place the whole data flow is drawn: `broker/FLOWS.md`

A correction to a previous conclusion goes in the incident log with the date,
in the same entry as the conclusion it corrects, so a reader finds both.
