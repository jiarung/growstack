# Contributing

This repo is maintained by one person and one or more AI agents, and most of
its history is the two of them correcting each other. The rules below are the
ones that came out of that. They apply to agents and humans alike, and outside
patches are welcome on the same terms: open a PR with a message that would
survive the review described here.

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
   - **codex**, adversarially, from the repo root:
     ```
     codex exec -C . -m gpt-5.5 --sandbox read-only \
       'Adversarial review of commit <hash> (git show <hash>). …' < /dev/null
     ```
     `< /dev/null` matters: codex appends piped stdin to the prompt. `-m`
     pins the model because the account-side default has changed under us
     before. In the prompt, name the files, say what the change claims, list
     the specific things to hunt for, ask for file:line. Ask for defects, not
     approval; tell it to say "fine" for what it checked and found fine, so
     silence is not ambiguous.
   - **the maintainer**, who reads the message and the diff.
3. **Fix what the review finds.** As a follow-up commit by default, so the
   review and the response are both visible in history. Amend or fixup is
   allowed only if the commit under review has not been pushed — and the
   amended hash is then presented for review again, since it is a different
   commit. The fix's message says what the review found and what was done;
   "Codex, on `<hash>`: …" is the convention. A finding that turns out to be
   wrong is also recorded, with why.
4. **Push when told to, and only what was told.** "commit and push" means
   both, for the commit(s) that instruction describes. It does not carry
   forward: a review-fix commit made afterwards waits for its own push
   instruction, unless the instruction already covered it ("push once the
   review is addressed"). "commit" alone means commit. An agent that is
   unsure asks.

A review that comes back clean is still recorded in the next message that
touches the area ("Everything else in the review came back fine: …"), so
the next reader knows what was already checked.

## The message itself

Subject line: a lower-case type prefix (`feat:`, `fix:`, `docs:`, `test:`),
then a specific, descriptive phrase — the one-line changelog. This repo's
subjects read like a sentence about what happened, not a command. The body says
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

A correction to a previous conclusion goes in the incident log, dated. If
the conclusion already lives in an entry, correct it there — a note at the top
of that entry, so a reader finds both. If it lives elsewhere (a plan, a
README), add a new dated entry and cross-link from the place that was wrong.
