# claude-scout-starter

A weekly agent that scans **Reddit, GitHub, Hacker News, dev.to, and
Lobste.rs** for AI-agent / Claude Code / MCP / your-domain content, then
asks Claude to score each candidate against **your specific stack** —
so you get a ranked newsletter that says _"for your setup, this is a 9/10
adopt this week; here's how to wire it in; here are three things that will
break"_ instead of a generic "here's what's trending" feed.

Output: a sectioned Markdown newsletter emailed to you plus a Slack
summary. Runs weekly via cron / Windows Task Scheduler.

Cost: about **$0.50 per weekly run** on Claude Sonnet 4.6 (20 items × ~2¢
each). No database, no containers, no cloud infra beyond the public APIs
it reads.

> **Why this exists — read this first**
> [Claude Scout — how we built it (explainer)](https://claude.ai/code/artifact/2c227be6-addd-4b9c-958f-755690365608) — the design writeup for someone AI-savvy but new to implementation. Covers the problem, the seven-step loop, the vet prompt design (the clever bit), a real sample of the newsletter output, and the full stack in one paragraph. Read this before the code.

---

## Setup (10 minutes)

### 1. Prerequisites
- Python 3.10+
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com))
- (Optional) A Gmail account with an [App Password](https://support.google.com/mail/answer/185833) for the outbound newsletter
- (Optional) A Slack bot token if you want the summary posted to a channel
- (Optional) A GitHub Personal Access Token to raise the API rate limit from 60 to 5,000 req/hour

### 2. Install
```bash
git clone <this-repo-url>
cd claude-scout-starter
python -m venv .venv
source .venv/bin/activate    # or: .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### 3. Configure your secrets
```bash
cp .env.example .env
# Edit .env and fill in ANTHROPIC_API_KEY (required) plus any optional ones.
```

### 4. **The important step — describe your stack**
```bash
cp stack.example.md stack.md
# Edit stack.md and rewrite every line to describe YOUR actual setup.
```
This file is the system-prompt context Claude uses to grade every
candidate. The specificity of this file is the single biggest lever on
how useful the newsletter will be. Include your OS, language, scheduler,
current agents, domain, live constraints, and what "adopt" means for you.

### 5. Test it
```bash
python scout.py --dry-run --max 5
```
This runs a small sweep, grades 5 items, and prints the newsletter to
stdout without sending email or Slack. Read the output. If Claude is
grading items in ways that don't reflect your world, your `stack.md`
needs more detail.

### 6. Run it for real
```bash
python scout.py
```

---

## What each file does

| File | Purpose |
|---|---|
| `scout.py` | The whole agent — one file, ~500 lines. Edit the CONFIG block at the top to change the source lists (subreddits, GitHub queries, dev.to tags) and the RELEVANT_KEYWORDS filter. |
| `stack.md` | The system-prompt context. **The most important file.** Describes your world so Claude's grading is grounded, not generic. |
| `stack.example.md` | Template for `stack.md`. Copy it and edit. |
| `.env` | Your secrets. Never commit. |
| `.env.example` | Template with the env var names filled in. |
| `requirements.txt` | Python deps: `requests`, `anthropic`. |
| `reports/scout_latest.md` | The latest newsletter markdown (generated). |
| `reports/scout.log` | Debug log (appended to each run). |

---

## Scheduling

### macOS / Linux (cron)
```bash
crontab -e
# Add this line to run every Monday at 7am:
0 7 * * 1 cd /path/to/claude-scout-starter && ./.venv/bin/python scout.py >> reports/cron.log 2>&1
```

### Windows (Task Scheduler)
Create a `.bat` wrapper:
```bat
@echo off
cd /d C:\path\to\claude-scout-starter
call .venv\Scripts\activate.bat
python scout.py >> reports\scout.log 2>&1
```
Then:
```powershell
schtasks /Create /TN "Scout Weekly" `
  /TR "C:\path\to\claude-scout-starter\run_scout.bat" `
  /SC WEEKLY /D MON /ST 07:00 /F
```

### GitHub Actions
Add a workflow at `.github/workflows/scout.yml`:
```yaml
name: scout
on:
  schedule:
    - cron: '0 12 * * 1'  # Mondays at 12:00 UTC
  workflow_dispatch:
jobs:
  scout:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt
      - run: python scout.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GMAIL_USER: ${{ secrets.GMAIL_USER }}
          GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
          EMAIL_TO: ${{ secrets.EMAIL_TO }}
          # Optional:
          SLACK_BOT_TOKEN: ${{ secrets.SLACK_BOT_TOKEN }}
          SLACK_CHANNEL: ${{ secrets.SLACK_CHANNEL }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

---

## Adapting to a different domain

The scout is domain-agnostic. To scout a different topic space, edit three
things in `scout.py`:

1. **`REDDIT_SUBREDDITS`** — swap in subs for your domain
2. **`GITHUB_QUERIES`** — swap in search terms for your domain
3. **`DEVTO_TAGS`** — same idea
4. **`RELEVANT_KEYWORDS`** — the coarse keyword filter that drops non-topical hits
5. **`stack.md`** — rewrite so Claude grades against your world

Everything else (dedupe, rank, vet, render, deliver) stays the same.

---

## How the vetting actually works

For each of the top ~20 candidates, `scout.py` calls Claude Sonnet with:
- A **system prompt** that includes the entire contents of your `stack.md`
- A **user message** with the candidate's source, title, URL, score, and body/README

Claude returns one JSON object per item with fields: `tag`
(adopt/watch/case_study/prompt/trending), `adopt_score` (1-10),
`what_it_is`, `use_cases`, `extracted_prompts`, `adoption_sketch`,
`gotchas`, `build_ideas`, `scenarios`, and a couple more.

The renderer groups by tag, sorts by score, and produces a Markdown
newsletter with one section per bucket. Same shape every week.

The `--catchup` flag widens the lookback to 180 days and up to 40 items
per run — useful for a first-time sweep to see what's been shipped
recently.

---

## Cost

- Claude Sonnet 4.6: ~$3/M input, $15/M output
- Per-item vet: ~1,500 input tokens (system prompt + candidate) + ~600 output tokens ≈ **~$0.014**
- 20 items × 1 run/week × 4 weeks = **~$1.10/month**

Add `--max 40` or `--catchup` and you'll roughly double that on the run
it fires. The `LOG_PATH` shows per-run item count for post-hoc
reconciliation.

---

## What next

The generic version stops after "email you the newsletter". Extensions
worth building on top:

- A **weekly diff** — compare this week's newsletter against last week's,
  post only the delta to Slack.
- **Cross-run memory** — a JSONL log of everything that's been vetted so
  the same repo doesn't get scored twice. Feed the log back into the vet
  prompt so Claude knows what's already been considered.
- **Auto-adopt flow** — for items scored 9/10 ADOPT, auto-open a GitHub
  issue on your own repo with the `adoption_sketch` pre-filled.
- **A domain-specific twin** — run the same scout with a different
  `stack.md` for a different area (e.g. one for AI agents, one for your
  industry).

---

## License

MIT. Steal it, fork it, ship it.
