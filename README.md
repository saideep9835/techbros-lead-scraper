# Tech Bros AI Studio — Daily Lead Scraper

Finds new local business leads every day (no website / low online presence)
using Google Maps data via Apify, dedupes against everything you've already
found, and saves the new ones to a CSV.

Runs two ways — pick whichever fits you:
- **Web app (the main way):** a local dashboard where you pick keywords and
  cities in a form, watch the scrape run live, and browse/export the results.
- **Command line:** run the scripts directly.

> **There is no daily schedule, on purpose.** Apify bills per result returned,
> not per *new* lead — so re-running the same search every day costs full price
> to rediscover businesses you already have. Search on demand from the dashboard
> instead, and widen the keywords or cities when you want more.
>
> A manual GitHub Action is still available (Actions tab → *Lead Scrape
> (manual)* → Run workflow) if you ever want a scrape without your laptop on.

---

## 0. The web app (start here)

First, set up your credentials once — copy the template and fill it in:

```bash
cp .env.example .env
```

Open `.env` and set at minimum:
- `APIFY_TOKEN` — your Apify Personal API token
- `APP_PASSWORD` — the password you'll use to log into the dashboard
- `FLASK_SECRET_KEY` — generate one with
  `python -c "import secrets; print(secrets.token_hex(32))"`

Then start it. **With Docker** (recommended — no venv, keeps running in the
background, survives reboots):

```bash
docker compose up -d --build
```

Or **without Docker**:

```bash
source venv/bin/activate
python app.py
```

Either way, open **http://127.0.0.1:8080** and sign in with your `APP_PASSWORD`.

### Docker day-to-day

```bash
docker compose up -d          # start (or restart after a code change)
docker compose logs -f        # watch what it's doing
docker compose restart        # pick up an edited template or config
docker compose down           # stop it
docker compose up -d --build  # rebuild after changing requirements.txt
```

Your leads live in `./data` on your Mac, mounted into the container — so
`docker compose down` does **not** lose anything. `config.json` is mounted too,
so you can edit a search without rebuilding.

The container binds to `127.0.0.1` only, so nothing outside your machine can
reach it. It runs gunicorn (not Flask's development server) as a non-root user,
with a single worker on purpose: scrapes run as background threads, SQLite takes
one writer, and the login rate limiter keeps counts in memory — multiple workers
would break all three.

> `.env` is gitignored, so it never gets committed. `.env.example` is the
> template that *does* get committed — **never put real values in it.** Every
> script here (`app.py`, `scraper.py`, `outreach.py`) reads `.env` automatically,
> so you never need to `export` anything.

- **New Search** — type in your business types and cities, set results per
  search, and it shows the estimated cost *before* you run anything. Runs are
  capped at 1,000 results so a typo can't spend hundreds of dollars.
- **Live progress** — watch each search complete, with a running count of new
  vs. already-known leads. You can cancel mid-run.
- **Leads** — filter by city, category, or "no website only", and download the
  filtered set as CSV.
- **Dashboard** — totals, how many have no website, how many are callable, how
  many are high priority, and breakdowns by city and category.

> `APP_PASSWORD` is required — the app refuses every login until you set one,
> because anyone who reaches the page can spend your Apify credit. Keep it bound
> to `127.0.0.1` (the default) unless you've put it behind HTTPS and a firewall.
>
> Setting `FLASK_SECRET_KEY` keeps you logged in across restarts; without it a
> random key is generated each launch and every restart logs you out.

The app writes the **same CSVs** as the command-line scripts and reads
`leads_master.csv` back in at startup, so the web UI, the CLI, and the daily
GitHub Action all stay in sync and share one deduplicated lead list.

---

## 1. Get an Apify account + token

1. Sign up free at https://apify.com
2. Go to **Settings → Integrations** in the Apify Console
3. Copy your **Personal API token**

You get some free usage credit monthly; after that, this actor costs
roughly **$0.01 per lead result** (see `config.example.json` for a cost estimate
based on your settings).

---

## 2. Configure your search

Copy the example config and edit it:

```bash
cp config.example.json config.json
```

Open `config.json` and edit:
- `keywords` — business types to search (e.g. "restaurant", "plumber")
- `cities` — locations to search, e.g. `"Princeton, TX"`
- `max_results_per_query` — how many results per keyword+city combo
- `lead_filter` — `ALL`, `HOT_ONLY`, or `HOT_WARM` (HOT = higher-priority prospects)

The default config is already set up for Princeton, McKinney, Anna, Melissa, and
Farmersville, TX across 5 common local-business categories (~50 leads/run, ~$0.50).

---

## 3. Run it locally (optional, to test)

**First, create a virtual environment** (keeps this project's Python packages
separate from everything else on your machine):

```bash
# macOS / Linux
python3 -m venv venv
source venv/bin/activate

# Windows (PowerShell)
python -m venv venv
venv\Scripts\Activate.ps1

# Windows (Command Prompt)
python -m venv venv
venv\Scripts\activate.bat
```

You'll know it worked because your terminal prompt will show `(venv)` at the start.

**Then install dependencies and run:**

```bash
pip install -r requirements.txt

# Credentials come from .env — see step 0 above. No exports needed.
python scraper.py
```

**Each time you come back to work on this later**, you only need to
re-activate the virtual environment (no need to recreate it):

```bash
source venv/bin/activate        # macOS/Linux
venv\Scripts\Activate.ps1       # Windows PowerShell
```

When you're done, type `deactivate` to exit the virtual environment.

> Note: GitHub Actions (Step 4 below) doesn't need any of this — it creates
> its own clean environment automatically every time it runs.

New leads are saved to:
- `data/leads_master.csv` — running list of every lead ever found (deduped)
- `data/new_leads_YYYY-MM-DD.csv` — just today's new leads, ready for outreach

---

## 4. Running it in the cloud (optional, manual)

The repo includes a GitHub Actions workflow for scraping without your laptop on.
**It has no schedule** — you trigger it yourself.

1. Push this folder to a **private** GitHub repository.
2. Go to **Settings → Secrets and variables → Actions** → **New repository secret**
   and add `APIFY_TOKEN` (your Apify token from Step 1).
3. Run it from the **Actions** tab → *Lead Scrape (manual)* → **Run workflow**.
   Runs default to **dry run**, so they won't place real calls unless you untick
   that box.

The workflow commits whatever it finds into `data/` in your repo. To see those
leads on your local dashboard, `git pull` and restart `app.py` — it imports
`leads_master.csv` at startup.

> **Why no daily schedule?** Apify charges per result returned, not per new
> lead. The same search run daily returns the same businesses, which get
> deduplicated away — so you'd pay full price for almost nothing. Search on
> demand from the dashboard instead. If you later want a schedule, add a
> `schedule:` trigger back to the workflow, but rotate the keywords or cities
> so each run covers fresh ground.

> Keep the schedule inside your calling window. `outreach.py` only dials during
> the `business_hours_start`–`business_hours_end` range in
> `outreach_config.json` (default 9–5, Mon–Fri), so a run scheduled outside
> that window will scrape leads but never call anyone.

---

## 5. Using the leads

Each row includes: business name, phone, address, website (blank = no website
found), rating, review count, lead priority, and a direct Google Maps link.

Before doing outreach, run the prioritizer — it scores each lead and sorts the
best prospects to the top, so you contact them in the right order:

```bash
python prioritize_leads.py
```

That writes `data/new_leads_prioritized_YYYY-MM-DD.csv`, adding three columns:
- `priority_score` — higher is better (great reviews + no website scores highest)
- `pitch_gap` — the specific opening line for that lead
- `recommended_pitch` — `website` or `ai_services`

`outreach.py` picks this file up automatically when it exists.

The full pipeline is:

```bash
python scraper.py           # find new leads
python prioritize_leads.py  # score + sort them
python enrich.py            # AI talking points / email drafts
python outreach.py --dry-run   # preview who'd be contacted
python outreach.py          # actually call/email
```

---

## 5b. Enrich leads with Gemini

`enrich.py` reads each business's website and writes outreach material tailored
to that specific business. Add `GEMINI_API_KEY` to your `.env` (get one at
https://aistudio.google.com/apikey), then use the **Enrich** tab in the web app,
or run it directly:

```bash
python enrich.py              # up to 25 un-enriched leads, best first
python enrich.py --limit 100
python enrich.py --dry-run    # fetch sites, call no API
python enrich.py --force      # re-analyze leads already done
```

What it produces depends on how the lead can be reached:

| Lead has… | You get |
|---|---|
| A phone number | `call_opener`, `business_summary` and `talking_points`, passed to the Retell agent as dynamic variables |
| No phone number | A **draft email**, saved for review under the **Drafts** tab — never sent |

**Results are cached.** A lead with `enriched_at` set is never re-analyzed, so
running this repeatedly never pays twice for the same business. Failures are
recorded without destroying content you already had, and the lead is retried
on the next run.

Enrichment is optional — `outreach.py` falls back to the deterministic
`pitch_gap` line for any lead Gemini hasn't seen, so calling never depends on
having run this.

---

## 6. Automated outreach (calls + emails)

`outreach.py` runs after the scraper and the prioritizer, and automatically:
- **Calls leads with a phone number** using an AI voice agent (Retell AI) that
  asks a qualifying question, then pitches Tech Bros if there's a fit.
- **Emails leads with no phone number** — it checks their website for a
  contact email and sends an outreach email if one is found.
  **Off by default** — see the email warning below.
- **Logs anything it couldn't contact** (no phone, no findable email) to
  `data/manual_followup.csv` for you to handle by hand.

Three safety rails are always on:
- **Never calls the same business twice.** Anyone already in
  `data/outreach_log.csv` is skipped on later runs.
- **Never calls anyone on your opt-out list.** Add a phone number, email, or
  whole domain to `data/do_not_contact.csv` the moment someone asks to be
  left alone.
- **Caps each run.** `max_calls_per_run` (default 5) stops a bad config from
  dialing your entire list at once, and calls are spaced out by
  `seconds_between_calls`.

### Setup

1. **Create a Retell AI account** at https://retellai.com
2. **Create an Agent** in the Retell dashboard. Use `retell_agent_prompt.txt`
   in this project as your starting system prompt — it includes the
   qualifying question + pitch flow already written out.
3. **Get a phone number** to call from — either buy one directly in Retell,
   or connect a Twilio number (Retell supports both).
4. **Copy the config:**
   ```bash
   cp outreach_config.example.json outreach_config.json
   ```
   Edit `outreach_config.json`: paste your Retell Agent ID and phone number,
   and adjust the business-hours window / email template as you like.
5. **Set up Gmail for auto-sending email:**
   - Go to https://myaccount.google.com/apppasswords and create an "App
     Password" for your Gmail account (this is NOT your normal password).
6. **Add your credentials to `.env`** (locally) or as GitHub Secrets (for
   automation — see step below):
   - `RETELL_API_KEY` — from Retell dashboard → API Keys
   - `EMAIL_ADDRESS` — your Gmail address
   - `EMAIL_APP_PASSWORD` — the app password from step 5

### Run it locally — always dry-run first

```bash
python outreach.py --dry-run
```

This prints exactly who would be called, in what order, and with what
information passed to the AI agent. It places **no calls, sends no mail, and
writes no logs**. Check that list carefully — then drop the flag to go live:

```bash
python outreach.py
```

By default it processes today's prioritized CSV (falling back to the plain
`new_leads_YYYY-MM-DD.csv`). You can point it at any CSV manually:
`python outreach.py data/some_other_file.csv`, and cap a single run with
`--limit 3`.

**For your very first live call, call yourself.** Put one row with your own
phone number in a test CSV and run against that, so you hear exactly what a
prospect hears before a real business does.

### Add it to the daily automation

Add these repository secrets (same place as `APIFY_TOKEN` — Settings →
Secrets and variables → Actions):
- `RETELL_API_KEY`
- `OUTREACH_CONFIG_JSON` — the entire contents of your `outreach_config.json`,
  pasted in as one secret. The outreach step is **skipped entirely** unless
  both of these exist, so the workflow can never dial out with the
  placeholder values from the example config.
- `EMAIL_ADDRESS` / `EMAIL_APP_PASSWORD` — only needed if you turn email on.

### Important notes

- **Without `--dry-run` this places real calls with no review step.** Watch
  the first several days of `data/outreach_log.csv` closely.
- **Business hours are enforced by default** (Mon–Fri, 9am–5pm in your
  configured timezone) — calls outside that window are deferred to
  `data/manual_followup.csv` rather than skipped.
- **Email is disabled by default** (`enable_email: false`). US CAN-SPAM law
  requires a working opt-out and a real physical postal address on commercial
  email — `outreach.py` appends both automatically, but you must put your real
  address in `company_postal_address` first. Be aware that emailing addresses
  scraped off websites carries real risk to your Gmail account's standing;
  a proper email service is the safer path if you scale this up.
- **Compliance:** AI outbound calling to businesses may be subject to
  telemarketing/robocall rules depending on your state — this isn't legal
  advice, but the agent prompt identifies itself honestly when asked, calls
  run only in business hours, and repeat calls are blocked automatically.
  Worth a quick compliance check before scaling past testing.
- **Honor opt-outs immediately.** When someone asks not to be contacted, add
  them to `data/do_not_contact.csv` right away.

---

## 7. Extending this later

- **Auto-email new leads to yourself:** add a step to the GitHub Actions
  workflow using an action like `dawidd6/action-send-mail` with the CSV attached.
- **Push to Google Sheets instead of CSV:** swap the CSV-writing code in
  `scraper.py` for the `gspread` library.
- **Auto-send first outreach message:** connect the CSV/Sheet to Zapier or
  Make.com to trigger an email/SMS the moment a new HOT lead appears.

---

## Files in this project

```
techbros-lead-scraper/
├── Dockerfile  docker-compose.yml      # Run it as a container (recommended)
├── app.py                              # Web UI — search form, dashboard, lead browser
├── db.py                               # SQLite store behind the web UI
├── templates/  static/                 # Web UI pages and styling
├── scraper.py                          # 1. Finds new leads
├── prioritize_leads.py                 # 2. Scores + sorts them best-first
├── enrich.py                           # 3. Gemini: call notes / email drafts
├── outreach.py                         # 4. Auto-calls (Retell AI) or emails leads
├── retell_agent_prompt.txt             # Starting prompt for your Retell AI agent
├── config.example.json                 # Copy to config.json and edit (scraper settings)
├── outreach_config.example.json        # Copy to outreach_config.json and edit (outreach settings)
├── requirements.txt                    # Python dependencies
├── data/
│   ├── leads_master.csv                # Every lead ever found (deduped)
│   ├── new_leads_YYYY-MM-DD.csv        # Today's new leads
│   ├── outreach_log.csv                # Who was contacted, when (prevents repeat calls)
│   ├── manual_followup.csv             # Couldn't auto-contact — handle by hand
│   └── do_not_contact.csv              # Your opt-out list — add people here
├── .github/workflows/daily-scrape.yml  # Runs the whole pipeline daily
├── skill/                              # Claude skill version of this workflow
└── README.md                           # This file
```
