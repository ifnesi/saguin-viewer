# Contributing to saguin-viewer

Contributions are welcome, and the barrier is deliberately low. This
document is short because it should be.

This repository holds two viewers for a
[Sagüin](https://github.com/ifnesi/saguin) broker - a browser one in `web/`
and a terminal one in `cmd/saguin-viewer`. Neither is part of the broker:
both are clients of the operations listener
[RFC 0005](https://github.com/ifnesi/saguin/blob/main/docs/rfcs/0005-operations.md)
specifies, and that document is the specification for everything here.

## The most useful thing you can do today

Both viewers are early, and the specification is ahead of them. So the
highest-value contribution available right now is:

**Read RFC 0005 beside a screen and tell us where the screen is wrong.**

No Go, no Python, no broker of your own - a careful reading of what a panel
claims against what the catalogue actually promises. A number labelled as
something it is not, a metric drawn as a zero where the broker published
nothing, a card that would draw an empty panel on any broker but the one it
was written against: each is a defect, and each has been one here.

Also welcome: documentation fixes, dashboards, and tests that assert
something RFC 0005 states which nothing currently checks.

## The one rule that is unusual

**Behaviour is specified before it is implemented, and the specification is
in the other repository.** Every number either viewer draws is a metric or a
route RFC 0005 names. If a change needs one that RFC 0005 does not state,
the RFC changes first - in saguin, in an earlier pull request. Inventing a
metric here is how a viewer and a broker quietly stop agreeing about what a
number means.

Most changes need no RFC edit:

| Change | RFC amendment in saguin? |
|---|---|
| Bug fix, so a viewer matches what RFC 0005 already says | No |
| Tests, docs, comments, dashboards, refactors | No |
| A column or panel computed from metrics RFC 0005 names | No |
| Drawing a metric the catalogue does not carry | **Yes** |
| Reading a route the operations listener does not serve | **Yes** |
| Resolving an ambiguity you found in RFC 0005 | **Yes** - that *is* the fix |

Unsure? Open an issue and ask before writing code.

## Two things that will get a change sent back

**A second copy of the catalogue.** The metric names a viewer knows are
checked against RFC 0005 and against a real broker, in both directions, by
the drift test in `web/tests`. A change that adds a name in one place and
not the others, or that exempts a name from that check, is the drift the
test exists to stop.

**A derived number presented as the broker's.** A viewer may work a number
out - consumer lag is a subtraction and no metric can carry it - but what it
computes has to be visibly its own. A column named like a metric, or a
figure that reads as something the broker published when it is not, is worse
than not showing it.

## Conventions in the code

- **Every rule a viewer enforces has a test, and the test names the RFC
  section it asserts** in its comment. A rule with no test is a rule nothing
  is holding down, and prose cannot fail where a test can.
- Tests assert the **failure**, not only the happy path, and a new test is
  watched failing without its fix before it is trusted.
- **A check reports on all of what it claims.** A sweep that quietly matched
  nothing passes by comparing nothing, so anything that counts says how much
  it counted and fails on a short list.
- **An unknown key is refused rather than ignored**, everywhere a file is
  read. A `metric:` written where `metrics:` was meant is a panel that draws
  nothing and says nothing.
- **A missing value is not a zero.** A metric with no sample for a channel
  is unknown, and drawing it as nought states something the broker did not.
- **The two viewers share nothing.** `web/` is Python with its own
  dependencies and `cmd/saguin-viewer` is Go with two; a change to one must
  not make the other's contributor install a toolchain to test it.
- **Code this project does not own carries its licence.** A Go dependency added
  to `go.mod`, or a file kept in `web/static/lib/`, needs its notice in
  `cmd/saguin-viewer/THIRD-PARTY-NOTICES.md`. A test on each side fails until it
  is there, and the Go half is compared against the module cache rather than
  trusted - a licence somebody retyped is one that can be wrong, and a wrong one
  asserts terms nobody granted.

## Conventions in the documents

- **State the failure, not just the rule.** Nearly every requirement exists
  because a plausible implementation would otherwise show a wrong number
  confidently. A rule without its failure gets "simplified" by the next
  reader.
- **Describe what is, not what was.** No rejected alternatives, no
  "previously", no "no longer". A decided trade-off is written as the
  decision and its cost. History is in `git log`.
- **Nothing new gets a new file.** New information goes into a document that
  already exists, or it does not get written.

## Issue or pull request?

A defect you are not fixing yourself, or an ambiguity in RFC 0005, starts as
an issue - and one about the specification belongs in saguin rather than
here. A fix that makes a viewer match what RFC 0005 already says can arrive
directly as a pull request.

## Pull requests

Fork, work on a short-lived branch, and open the pull request against
`main`. There is no `develop` branch and no branch naming convention. The
maintainer's changes arrive the same way.

One logical change each. Name the RFC 0005 sections it implements. In the
commit message, say *why* - the diff already says what.

Changes land by rebase or squash, never a merge commit, so the history stays
linear and each change keeps its reasoning in one commit. `main` is never
force-pushed: a commit id, once it exists, stays valid, and reviews and
records refer to commits by id. Delete the branch after the merge, or let
GitHub do it.

## Running the suites

    make test

The web viewer's suite needs a virtualenv; `web/README.md` creates it in two
lines. Some cases need saguin's own checkout, because both viewers are
clients of a broker and nothing here can stand in for one - they look for a
sibling `../saguin` with `bin/saguin` built, and `SAGUIN_REPO` names it
elsewhere. Absent, they skip and say what to build. **A skipped case is a
weaker run rather than a passing one**: those are what hold the viewers to a
broker instead of to a document.

## Releases

Releases are tags cut from `main`, so `main` is usually ahead of the latest
release. Install a release; building `main` gets you work in progress. A fix
to a released version, when `main` already carries newer work, is made on a
branch cut from that release's tag and tagged from there.

## Sign-off

Sign your commits with `git commit -s`, which adds a
[DCO](https://developercertificate.org/) line certifying you have the right
to submit the contribution. That is the whole process: no CLA, no account to
create, no company to involve. Apache-2.0 section 5 already places your
contribution under the project's licence.

## Conduct

Be decent to each other. Assume the person on the other end is trying to
help. Disagreement about a design is normal and welcome; contempt is not.
