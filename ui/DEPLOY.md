# Deploying wincurve to Vercel (private repo, `ui/` only)

The goal: a **public** URL, from a repo that stays **private**, serving **only** the
`ui/` folder — none of the Python, data, or scripts in the rest of the tree.

## Why Vercel and not GitHub Pages

GitHub Pages on the free plan requires the repository to be **public**. Vercel's free
**Hobby** plan deploys from a **private** GitHub repo and still gives a public URL, so the
code stays private while the site is open. That is the whole reason for the switch.

## One-time setup (≈2 minutes, in the Vercel dashboard)

1. **New Project → Import** `albertt76/wincurve` (authorize Vercel to read the private repo).
2. **Root Directory:** click *Edit* and set it to **`ui`**. This is the key step — Vercel
   then treats `ui/` as the site root and **only files inside `ui/` are ever served**.
   Everything else (`data/`, `nbaproj/`, `scripts/`, parquet files, caches) is never
   uploaded and never reachable by URL.
3. **Framework Preset:** *Other*. **Build Command:** leave empty. **Output Directory:**
   leave empty (the folder is already static HTML).
4. **Deploy.**

`ui/vercel.json` does the rest: it serves `projections.html` at the root URL (`/`) and
strips `.html` from paths. `ui/.vercelignore` keeps `build.py` and `template.html` out of
the deployment (the pattern matches at any depth, so `nhl/template.html` is excluded too).

**NHL impact viewer.** The self-contained `ui/nhl/impact.html` (skater xG-RAPM + goalie GSAx
leaderboards) is served on the **same project** — no separate deploy. With Root Directory = `ui`
and `cleanUrls`, it is reachable at **`/nhl/impact`**, and `vercel.json` adds a clean **`/nhl`**
rewrite. Rebuild it with `python scripts/nhl_build_impact_ui.py` (data inlined, like
`projections.html`); it needs no env vars or serverless function (no premium gate). Because the
project is GitHub-connected, a push to `main` auto-deploys it.

## No gate — team detail is public (2026-08-07)

There is **no premium gate and no password** anymore. `ui/build.py` inlines the **full** payload —
including each team's roster and the what-if grid — directly into `projections.html`, so every
team's expanded detail panel (per-player Off/Def and ≈Wins, RAPM flags, disagreement + conviction,
trade-undo, live editor) renders for everyone with no login. No env vars, no serverless function.
(A brief password gate existed 2026-08-02..08-06 via `ui/api/premium.js` + `PREMIUM_PASSWORD`; it
was removed. To bring a paid tier back, see the auth roadmap item in CLAUDE.md.)

## What is and isn't exposed

- **Served (public):** `ui/projections.html` (at `/`) — everything, including each team's roster,
  per-player Off/Def/≈Wins, and the what-if grid, all inlined (the same numbers the standalone
  `/players` leaderboard already exposes).
- **Not served:** the entire rest of the repo. With Root Directory = `ui`, Vercel has no
  access to `data/`, `nbaproj/`, or `scripts/` at deploy time.
- The **repo stays private** on GitHub; connecting it to Vercel does not make it public.

## Updating the site

Every push to `main` redeploys automatically. To refresh the numbers:

```
python scripts/project_current.py     # re-pull rosters, re-project the upcoming season
python scripts/fetch_market.py --refresh   # re-pull live Kalshi market lines
python scripts/build_snapshots.py     # rebuild all-season bundle (+ attach market)
python ui/build.py                     # inline full payload -> public projections.html
git commit -am "refresh projections + market" && git push
```

`ui/build.py` emits the single self-contained `ui/projections.html` (full payload inlined,
team detail public). Commit it.

Vercel picks up the push and redeploys in ~30 seconds.
