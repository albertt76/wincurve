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
the deployment, so the only file actually published is the self-contained
`projections.html` (all data inlined).

## What is and isn't exposed

- **Served:** `ui/projections.html` (at `/`). That file already inlines the projection data
  the app needs — the same numbers the app has always shown — so nothing new is exposed.
- **Not served:** the entire rest of the repo. With Root Directory = `ui`, Vercel has no
  access to `data/`, `nbaproj/`, or `scripts/` at deploy time.
- The **repo stays private** on GitHub; connecting it to Vercel does not make it public.

## Updating the site

Every push to `main` redeploys automatically. To refresh the numbers:

```
python scripts/project_current.py     # re-pull rosters, re-project the upcoming season
python scripts/fetch_market.py --refresh   # re-pull live Kalshi market lines
python scripts/build_snapshots.py     # rebuild all-season bundle (+ attach market)
python ui/build.py                     # inline into ui/projections.html
git commit -am "refresh projections + market" && git push
```

Vercel picks up the push and redeploys in ~30 seconds.
