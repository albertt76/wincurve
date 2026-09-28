# wincurve — multi-sport season-record projections

Guidance for Claude Code working in this repo. Shared, provider-neutral engineering guidance:
@AI_ENGINEERING.md

## Claude Code specifics

- `docs/nba/*.md` — NBA history and reference, one file per topic (not auto-loaded). Before
  touching a subsystem, read only the file that covers it; see the index at the bottom of
  AI_ENGINEERING.md. `grep -n` the file and read the matching section rather than all of it.
- `nhl/DESIGN.md` — the authoritative NHL doc (not auto-loaded); read its Status and Open items
  sections first for NHL work. NHL stage write-ups are in `docs/nhl/`.
- `docs/nba/roadmap.md` — stage status and open items. Update it in the same commit as related
  work, marking items ✅ / ⬜ as they change; move finished write-ups to `docs/nba/shipped.md`.

## Delegation

For a task that splits into bounded pieces, delegate implementation with the Agent tool instead of
doing everything in this session:

- **`quick-implementer` (Sonnet, low effort)** — routine, well-specified work: a described UI tweak
  in one template, a mechanical edit across scripts, a report script with a known shape, a doc
  update, boilerplate.
- **`implementer` (Sonnet, xhigh effort)** — implementation needing real judgment but still a
  bounded chunk: a new page following the UI conventions, a new `gate_*.py` whose design is already
  decided, porting a server calculation to the client, working in unfamiliar code.
- **Keep in this session**: anything that changes the shipped projection or decides whether a gate
  result ships, the market-is-downstream boundary, override judgment calls
  (`data/overrides/*.json`), architecture across sports, and reviewing/integrating delegated work.

Give a delegated task the objective, relevant files, constraints, and how to verify it — a subagent
has no memory of this conversation. Review important diffs before accepting them; "the subagent
reported success" is not verification on its own.

**Maintenance note:** audit and compact this file every few sessions — keep it small and precise.
Shared architecture, conventions, and engineering rules belong in `AI_ENGINEERING.md`; detailed
NBA history, experiment write-ups, and dated status belong in `docs/nba/`; NHL status belongs in
`nhl/DESIGN.md` and NHL stage write-ups in `docs/nhl/`. Do not grow this file,
`AI_ENGINEERING.md`, `nhl/DESIGN.md`, or `docs/nba/roadmap.md` with experiment write-ups.
