# wincurve — multi-sport season-record projections

Guidance for Claude Code working in this repo. Shared, provider-neutral engineering guidance:
@AI_ENGINEERING.md

## Claude Code specifics

- `docs/nba/*.md` — NBA history and reference, one file per topic (not auto-loaded). Before
  touching a subsystem, read only the file that covers it; see the index at the bottom of
  AI_ENGINEERING.md. `grep -n` the file and read the matching section rather than all of it.
- `nhl/DESIGN.md` — the authoritative NHL doc (not auto-loaded); read its Roadmap section first
  for NHL work.
- `docs/nba/roadmap.md` — stage status and open items. Update it in the same commit as related
  work, marking items ✅ / ⬜ as they change.

**Maintenance note:** audit and compact this file every few sessions — keep it small and precise.
Shared architecture, conventions, and engineering rules belong in `AI_ENGINEERING.md`; detailed
NBA history, experiment write-ups, and dated status belong in `docs/nba/`; NHL detail belongs in
`nhl/DESIGN.md`. Do not grow this file or `AI_ENGINEERING.md` with experiment write-ups.
