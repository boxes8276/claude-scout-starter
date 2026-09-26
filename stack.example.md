# Your stack — the heart of the vet prompt

This file is the single most important thing in the whole project. Rename it
to `stack.md` and rewrite it to describe YOUR actual world. Every candidate
Claude reads gets graded against this description, so specificity is
directly proportional to how useful the newsletter will be.

Below is a working example — replace every line with your real context.

---

## Who I am
Solo founder building a [what you do] business. I run [X] scheduled agents
on the side to automate [what]. I want situational awareness on new
Claude Code / Anthropic / AI-agent patterns AND on things happening in
[your domain, e.g. medical devices, law-firm ops, indie SaaS].

## Technical stack
- **OS**: macOS 14 (or Windows 10 / Ubuntu 22 / etc.)
- **Language**: Python 3.12
- **Scheduler**: cron (or Windows Task Scheduler / systemd / GitHub Actions)
- **LLM**: Anthropic Claude Sonnet 4.6 via the official SDK — I do NOT run
  local models.
- **Infra I DO have**: files on disk, a Cloudflare tunnel to my laptop,
  Gmail + Slack for outbound alerts, one small SQLite database.
- **Infra I DO NOT have**: Docker, Kubernetes, a managed database service,
  any cloud VMs, any public IPs. If a repo assumes any of these, flag it.

## Live constraints and current blockers
- My CRM (e.g. HubSpot / GHL / Airtable) grants me read-only scopes on
  conversations — I can't automate outbound SMS replies yet. Anything that
  might unblock this is high-priority.
- I'm on Anthropic's org tier with a 30K input-tokens/minute cap; a batch
  of >20 concurrent API calls will 429.
- I'm the only person on the team — no reviewer, no PR gate.

## Current agents (name them so Claude can reference them)
- `lead_scorer` — scores inbound leads each morning
- `outbound_sms` — drafts outreach SMS via the CRM API
- `deal_summary` — writes a 3-line deal recap after each call
- `weekly_kpi` — Sunday KPI email rollup
- (list yours; the more you name, the better Claude can propose fits)

## Domain
[Your business in 2-3 sentences. Include volumes so Claude can reason about
scale — "we track 2,000 active leads across two pipelines" is more useful
than "we do sales".]

## What "adopt" means for me
- Fits my no-Docker, files-only constraint (or has a clear file-based fallback)
- Windows/Mac/Linux compatible depending on your OS
- No paid managed service required to try it
- Under 200 lines of glue code to integrate
- Doesn't introduce a new language beyond Python
