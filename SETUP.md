# Setup — 5 minutes, no command line required

This repository is complete and tested. Nothing in it contains credentials,
API keys, account details, or personal information, and it will not run the
`gh` CLI or touch any other repository of yours.

---

## 1. Create the repository

Go to <https://github.com/new>

- **Repository name:** `polymarket-us-paper-agent`
- **Visibility:** **Public** (required — Actions minutes and GitHub Pages are
  only free on public repos)
- **Do not** tick "Add a README" — this bundle already has one.

Click **Create repository**.

## 2. Upload the files

On the empty repository page, click **uploading an existing file**.

Unzip `polymarket-us-paper-agent.zip` and drag **the contents** (not the
folder itself) into the browser — you should see `README.md`, `src/`,
`tests/`, `config/`, `docs/`, `state/`, and `.github/`.

> **Important:** GitHub's web uploader silently skips dotfolders in some
> browsers. After uploading, confirm that `.github/workflows/scan.yml` exists.
> If it is missing, click **Add file → Create new file**, type
> `.github/workflows/scan.yml` as the name, and paste in the contents of that
> file from the zip.

Click **Commit changes**.

## 3. Allow the workflow to write

**Settings → Actions → General → Workflow permissions**

Select **Read and write permissions**, then **Save**.

This is what lets the scan commit its audit log and disable its own schedule
at the 48-hour mark. Without it the job will run but fail to save state.

## 4. Turn on the dashboard

**Settings → Pages → Build and deployment → Source:** choose **GitHub Actions**.

## 5. Start the 48-hour evaluation

**Actions** tab → you may see a banner asking you to enable workflows on a
forked/uploaded repo; click **I understand my workflows, go ahead and enable
them**.

Select **Polymarket paper scan** in the left sidebar → **Run workflow** →
**Run workflow**.

That first manual run is the live test: it hits `gateway.polymarket.us` and
`api.weather.gov` for real, stamps the evaluation start time, and rebuilds the
dashboard. It takes about a minute.

---

## What happens next, without you

| When | What |
|---|---|
| Every ~10 min | Weather markets are re-discovered and re-evaluated, positions marked, every market considered is recorded, state committed |
| Continuously | Dashboard rebuilds at your Pages URL |
| At 48 hours | Final report is written, trading hard-stops, the state is committed, **and only then does the workflow disable its own schedule** |

Your dashboard will be at:

```
https://<your-github-username>.github.io/polymarket-us-paper-agent/
```

## Checking on it

- **Dashboard** — the URL above.
- **Did it actually run in the cloud?** The **Actions** tab lists every run with
  its timestamp, and each scan is a separate commit in the repository history.
  That commit history is the evidence the schedule kept running with your
  laptop closed.
- **Final report** — `state/final_report.json` appears at the 48-hour mark.
- **Sanity check on day one:** marked equity should sit just *below* $50 once
  positions are open, never above it. Open positions are valued at what they
  could actually be sold for, and you always buy at the ask and sell at the bid.
  Equity above $50 with nothing resolved would mean a marking fault — tell me.
- **Weather markets found.** The dashboard shows this count near the top, and
  the header reads "weather markets only". A low number is expected — Polymarket
  US lists temperature contracts for five cities. If it reads **0**, the weather
  strategy has nothing to work with — a red banner will say so, and the discovery table lists
  which query returned what. Send me that table.
- **Settlement banner.** If the dashboard shows a red "SETTLEMENT NEEDS
  ATTENTION" banner, a market finished but its outcome could not be read
  automatically. The position is deliberately left open rather than closed on a
  guess, and it is excluded from the performance figures. Send me the slug and
  I will look at the payload.
- **Is the 48-hour clock actually persisting?** Open `state/evaluation.json` in
  the repository. `started_at` must stay the **same value** across every scan
  commit. If it ever changes, the window is restarting and the run is invalid —
  tell me and I'll fix it. `runs_observed` should climb by one per scan.

## Stopping it early

Either one works on its own:

1. **Actions** tab → **Polymarket paper scan** → `···` → **Disable workflow**.
2. Edit `config/risk_config.json`, set `"emergency_stop": true`, commit.

## A note on the commit log

Every scan leaves a commit. That is deliberate: it is how you can see from
outside that the schedule is alive, and GitHub disables scheduled workflows
after 60 days of repository inactivity.

If you edit files in the repository while a scan is running, nothing breaks —
the scan adopts your change and re-applies its own results on top. The one case
it refuses is another scan having written `state/` in the meantime; it then
drops its own results with a warning rather than overwrite them, and the next
scan picks up from the newer state.

## If something goes wrong

Open the **Actions** tab and click the failed run — the logs are plain English.
The most common causes are step 3 (workflow permissions not set to read/write)
and step 4 (Pages source not set to GitHub Actions). Send me the error text and
I'll fix it.

---

## What this will and will not do

It **will** trade a $50 pretend bankroll against real Polymarket US prices and
real NOAA station data, and log every decision with its reasoning.

This run is **weather-only**: it scans Polymarket US temperature contracts and
nothing else. You will see a small number of markets per scan — that is
correct, not a fault. The broad all-markets scan is deliberately switched off,
since nothing outside weather is auto-traded anyway.

It evaluates **both sides** of each market and will paper-trade NO as readily as
YES. It reads the weather date from the contract text rather than from the
settlement date, and it skips any market whose date, threshold, station, or
settling Climate Report it cannot pin exactly.

It **will not** place a real order. There is no order-entry code in this
repository, no authenticated Polymarket client, and no API key anywhere. A test
(`test_no_order_placement_code_anywhere`) fails the build if that ever changes.
It also never contacts `polymarket.com`, the global exchange you cannot legally
trade from Pennsylvania — enforced by `test_no_polymarket_dot_com_anywhere`.
