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

## Premium gate — set the password (required for the "Unlock details" feature)

The app is split into a **public** payload (inlined in `projections.html`: the projection
bars, ranges, off/def and RAPM-arm readouts, market comparison, Track-record and Drift views,
glossary) and a **premium** payload (each team's roster + what-if grid, which drive the
expanded detail panels: per-player Off/Def and ≈Wins, RAPM flags, disagreement + conviction,
trade-undo, and the live editor). The premium payload lives in the serverless function
`ui/api/premium.js` and is returned **only** when the request password matches an env var.

5. **Add the password.** Project → *Settings → Environment Variables* → add
   **`PREMIUM_PASSWORD`** = your chosen passphrase (Production + Preview). Redeploy so the
   function picks it up. Without it, `/api/premium` returns 500 ("gate not configured") and
   the details stay locked for everyone.

To rotate the password, change the env var and redeploy — no rebuild needed (the check is
server-side). The passphrase is shared (one for all visitors); per-user accounts + billing
are the documented next step (see CLAUDE.md).

## What is and isn't exposed

- **Served (public):** `ui/projections.html` (at `/`) — bars, ranges, readouts, track record,
  drift, glossary. It inlines only the **public** payload; the rosters/grids are **not** in it
  (verify with view-source).
- **Gated:** the premium payload is embedded in `ui/api/premium.js`, a Vercel **Serverless
  Function** — Vercel runs `api/*.js` as functions, so the file's source is never served as a
  static asset. It returns the data only on the correct `PREMIUM_PASSWORD`.
- **Not served:** the entire rest of the repo. With Root Directory = `ui`, Vercel has no
  access to `data/`, `nbaproj/`, or `scripts/` at deploy time.
- The **repo stays private** on GitHub; connecting it to Vercel does not make it public.

## Updating the site

Every push to `main` redeploys automatically. To refresh the numbers:

```
python scripts/project_current.py     # re-pull rosters, re-project the upcoming season
python scripts/fetch_market.py --refresh   # re-pull live Kalshi market lines
python scripts/build_snapshots.py     # rebuild all-season bundle (+ attach market)
python ui/build.py                     # split -> public projections.html + gated api/premium.js
git commit -am "refresh projections + market" && git push
```

`ui/build.py` now emits **two** artifacts: the public `projections.html` and the
password-gated `ui/api/premium.js` (with the premium data embedded). Commit both.

Vercel picks up the push and redeploys in ~30 seconds.
