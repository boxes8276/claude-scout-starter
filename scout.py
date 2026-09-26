"""scout.py — a weekly agent that scans Reddit, GitHub, Hacker News, dev.to,
and Lobste.rs for content in a topic space you care about, then asks Claude
to grade each item against YOUR SPECIFIC STACK.

The output is a ranked, sectioned newsletter (ADOPT / CASE STUDIES / PROMPTS
/ WATCH / TRENDING) delivered by email + optional Slack summary.

WHY THIS IS DIFFERENT FROM A GENERIC "trending on GitHub" FEED
- Grounded per-item scoring: Claude reads each candidate with a long
  system prompt describing YOUR real stack (OS, language, constraints,
  live blockers). It answers "is this adoptable THIS WEEK for me?" — not
  "is this cool in the abstract?".
- Structured JSON output means the newsletter is scannable, sortable, and
  every item has the same shape (adopt_score, mhp_use_cases, gotchas...).

SETUP
1. pip install -r requirements.txt
2. Copy .env.example to .env and fill in your keys.
3. Edit stack.md — this is the most important step. It is the system-prompt
   context Claude uses to grade every candidate. Details in README.md.
4. Optionally edit the CONFIG block below to change source lists / keywords.
5. Test:  python scout.py --dry-run --max 5
6. Schedule (see README.md for cron / Task Scheduler examples).

CLI
  python scout.py                # normal run — email + Slack + save latest.md
  python scout.py --dry-run      # print to stdout, no email/Slack
  python scout.py --max 20       # cap newsletter (default 20)
  python scout.py --days 14      # widen lookback (default 7)
  python scout.py --catchup      # 180-day sweep, up to 40 items
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests

# Anthropic SDK is only imported when we need it, so `--dry-run` without
# candidates can still work without the key set.
try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None  # type: ignore


# ============================================================
# CONFIG — edit these to match your topic space
# ============================================================

# Subreddits to scan. Mix your ecosystem, your domain, and adjacent tools.
REDDIT_SUBREDDITS = [
    "ClaudeAI", "Anthropic", "LocalLLaMA", "MachineLearning",
    "AI_Agents", "ChatGPTCoding", "programming", "Python",
    # Add your domain-specific subs here (e.g. "gohighlevel", "medicaldevices")
]

# GitHub search queries. Each fires against api.github.com/search/repositories
# filtered to pushed:>=<days ago>.
GITHUB_QUERIES = [
    '"claude code" in:name,description,readme',
    'topic:claude-code',
    'topic:anthropic-claude',
    '"mcp server" claude in:name,description,readme',
    'topic:ai-agents',
    # Add your domain queries (e.g. 'gohighlevel in:readme')
]

# Hacker News full-text queries (Algolia).
HN_QUERIES = [
    "claude code", "anthropic claude", "MCP server claude",
    "ai agent framework", "multi-agent system",
]

# dev.to tag feeds (one API call per tag).
DEVTO_TAGS = ["claudecode", "claude", "ai", "agents", "llm"]

# Coarse keyword filter — any candidate whose title+body has zero hits gets
# dropped BEFORE going to Claude. Keep it broad but topical.
RELEVANT_KEYWORDS = [
    "claude code", "claude-code", "claudecode",
    "mcp", "model context protocol",
    "skill", "subagent", "sub-agent", "hook", "slash command",
    "agent sdk", "claude agent", "anthropic sdk", "claude api",
    "agent framework", "ai agent", "ai-agent", "multi-agent",
    "agent orchestration", "agentic", "autonomous agent",
    "langgraph", "langchain", "crewai",
    # Add your own domain keywords here
]

# Where to save the latest newsletter markdown (also picked up by any
# downstream agents you might build).
LATEST_PATH = Path(__file__).resolve().parent / "reports" / "scout_latest.md"

# Log file for debugging.
LOG_PATH = Path(__file__).resolve().parent / "reports" / "scout.log"

# The stack context file — this is the heart of the vet prompt.
STACK_MD_PATH = Path(__file__).resolve().parent / "stack.md"

# HTTP client identity — replace the email with yours so upstream APIs can
# reach you if you're hammering them.
USER_AGENT = "claude-scout-starter/1.0 (+contact: you@example.com)"
HTTP_TIMEOUT = 25

# Claude model to use for per-item grading. Sonnet is the sweet spot for
# cost/quality. Haiku is 5x cheaper but visibly worse on nuanced grading.
CLAUDE_MODEL = "claude-sonnet-4-6"

# Max runtime for the whole run in seconds. --catchup mode bumps this.
DEFAULT_MAX_RUNTIME_SEC = 600


# ============================================================
# ENV + LOGGING
# ============================================================

def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ. Silent if
    the file doesn't exist."""
    p = path or (Path(__file__).resolve().parent / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def event_log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] scout: {msg}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print(line, end="", flush=True)


# ============================================================
# HELPERS — inlined equivalents of standard utility functions
# ============================================================

def call_claude(api_key: str, system: str, user: str, max_tokens: int = 2200) -> str:
    """One-shot Anthropic Messages call. Returns the model's text reply."""
    if Anthropic is None:
        raise RuntimeError("anthropic package not installed — pip install anthropic")
    client = Anthropic(api_key=api_key)
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def send_gmail(user: str, app_password: str, to_addr: str,
               subject: str, body: str, as_html: bool = True) -> None:
    """SMTP send via smtp.gmail.com using an app password. Fails loudly."""
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject
    if as_html:
        msg.set_content(re.sub(r"<[^>]+>", "", body))
        msg.add_alternative(_markdown_to_html(body), subtype="html")
    else:
        msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, app_password)
        s.send_message(msg)


def _markdown_to_html(md: str) -> str:
    """Very light MD-to-HTML wrapper so Gmail renders headings and code
    blocks readably. Not a real Markdown parser — for casual reading only."""
    out = []
    in_code = False
    for line in md.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            out.append("<pre style='background:#f5f5f5;padding:8px;overflow:auto'>" if in_code else "</pre>")
            continue
        if in_code:
            out.append(line.replace("<", "&lt;").replace(">", "&gt;"))
            continue
        if line.startswith("### "):
            out.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("- "):
            out.append(f"<li>{line[2:]}</li>")
        elif line.strip() == "":
            out.append("<br>")
        else:
            out.append(f"<p>{line}</p>")
    return "<html><body style='font-family:sans-serif;max-width:780px'>" + "\n".join(out) + "</body></html>"


def post_slack(bot_token: str, channel: str, text: str) -> None:
    """Post a message to a Slack channel via chat.postMessage."""
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {bot_token}",
                 "Content-Type": "application/json; charset=utf-8"},
        data=json.dumps({"channel": channel, "text": text}),
        timeout=15,
    )
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError(f"slack: {r.json()}")


def is_relevant(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in RELEVANT_KEYWORDS)


def gh_headers() -> dict:
    h = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


# ============================================================
# SOURCES
# ============================================================

def fetch_reddit(subreddits: list[str], days: int, catchup: bool = False) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    endpoints = ("top.json?t=year&limit=100", "top.json?t=month&limit=100") if catchup \
        else ("new.json?limit=100", "hot.json?limit=50")
    out: list[dict] = []
    for sub in subreddits:
        for endpoint in endpoints:
            try:
                r = requests.get(f"https://www.reddit.com/r/{sub}/{endpoint}",
                                 headers={"User-Agent": USER_AGENT},
                                 timeout=HTTP_TIMEOUT)
                r.raise_for_status()
            except Exception as e:
                event_log(f"reddit r/{sub} {endpoint}: {type(e).__name__}: {e}")
                continue
            for child in r.json().get("data", {}).get("children", []) or []:
                d = child.get("data", {})
                created = datetime.fromtimestamp(d.get("created_utc") or 0, tz=timezone.utc)
                if created < cutoff:
                    continue
                title = d.get("title") or ""
                body = d.get("selftext") or ""
                if not is_relevant(title + " " + body):
                    continue
                out.append({
                    "source": f"reddit/{sub}",
                    "title": title,
                    "url": "https://www.reddit.com" + (d.get("permalink") or ""),
                    "score": d.get("score") or 0,
                    "comments": d.get("num_comments") or 0,
                    "created": created,
                    "summary": (body or "")[:3000],
                    "author": d.get("author"),
                })
            time.sleep(0.5)
    return out


def fetch_github(queries: list[str], days: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    out: list[dict] = []
    for q in queries:
        try:
            r = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": f"{q} pushed:>={cutoff}", "sort": "stars",
                        "order": "desc", "per_page": 25},
                headers=gh_headers(), timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            event_log(f"github query={q[:40]!r}: {type(e).__name__}: {e}")
            continue
        for repo in r.json().get("items", []) or []:
            text = (repo.get("name") or "") + " " + (repo.get("description") or "")
            if not is_relevant(text):
                continue
            pushed = repo.get("pushed_at") or repo.get("updated_at")
            try:
                pdt = datetime.fromisoformat((pushed or "").replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pdt = datetime.now(timezone.utc)
            out.append({
                "source": "github/repo",
                "title": repo.get("full_name") or repo.get("name") or "?",
                "url": repo.get("html_url"),
                "score": repo.get("stargazers_count") or 0,
                "comments": repo.get("open_issues_count") or 0,
                "created": pdt,
                "summary": (repo.get("description") or "")[:1000],
                "author": (repo.get("owner") or {}).get("login"),
                "_repo_full_name": repo.get("full_name"),
            })
    return out


def hydrate_readmes(items: list[dict], top_n: int = 8) -> None:
    """For top-N GitHub items by stars, fetch README text so Claude has
    something to reason over instead of just the one-line description."""
    gh_items = [it for it in items if it.get("source") == "github/repo"]
    gh_items.sort(key=lambda x: -(x.get("score") or 0))
    for it in gh_items[:top_n]:
        repo = it.get("_repo_full_name")
        if not repo:
            continue
        try:
            r = requests.get(
                f"https://api.github.com/repos/{repo}/readme",
                headers={**gh_headers(), "Accept": "application/vnd.github.raw"},
                timeout=HTTP_TIMEOUT,
            )
            if r.status_code == 200:
                it["summary"] = (it["summary"] + "\n\n=== README ===\n" + r.text[:2500])[:4000]
        except Exception as e:
            event_log(f"readme {repo}: {type(e).__name__}: {e}")


def fetch_hn(queries: list[str], days: int) -> list[dict]:
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    out: list[dict] = []
    for q in queries:
        try:
            r = requests.get(
                "https://hn.algolia.com/api/v1/search_by_date",
                params={"query": q, "tags": "(story,comment)",
                        "numericFilters": f"created_at_i>{cutoff_ts}",
                        "hitsPerPage": 50},
                headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            event_log(f"hn query={q!r}: {type(e).__name__}: {e}")
            continue
        for hit in r.json().get("hits", []) or []:
            title = hit.get("title") or hit.get("story_title") or hit.get("comment_text") or ""
            if not title or not is_relevant(title):
                continue
            created = datetime.fromtimestamp(hit.get("created_at_i") or 0, tz=timezone.utc)
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            out.append({
                "source": "hn", "title": title[:250], "url": url,
                "score": hit.get("points") or 0, "comments": hit.get("num_comments") or 0,
                "created": created,
                "summary": (hit.get("story_text") or hit.get("comment_text") or "")[:3000],
                "author": hit.get("author"),
            })
    return out


def fetch_devto(tags: list[str], days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for tag in tags:
        try:
            r = requests.get(
                "https://dev.to/api/articles",
                params={"tag": tag, "per_page": 30, "top": days},
                headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()
        except Exception as e:
            event_log(f"devto tag={tag!r}: {type(e).__name__}: {e}")
            continue
        for a in r.json() or []:
            try:
                created = datetime.fromisoformat((a.get("published_at") or "").replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if created < cutoff:
                continue
            title = a.get("title") or ""
            desc = a.get("description") or ""
            if not is_relevant(title + " " + desc):
                continue
            out.append({
                "source": f"devto/{tag}", "title": title, "url": a.get("url"),
                "score": a.get("public_reactions_count") or 0,
                "comments": a.get("comments_count") or 0,
                "created": created, "summary": desc[:2000],
                "author": (a.get("user") or {}).get("name"),
            })
    return out


def fetch_lobsters(days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict] = []
    for tag in ("ai", "programming"):
        try:
            r = requests.get(f"https://lobste.rs/t/{tag}.json",
                             headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            event_log(f"lobsters tag={tag}: {type(e).__name__}: {e}")
            continue
        for s in r.json() or []:
            try:
                created = datetime.fromisoformat((s.get("created_at") or "").replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if created < cutoff:
                continue
            title = s.get("title") or ""
            if not is_relevant(title):
                continue
            su = s.get("submitter_user")
            author = su.get("username") if isinstance(su, dict) else su
            out.append({
                "source": f"lobsters/{tag}", "title": title,
                "url": s.get("url") or s.get("short_id_url"),
                "score": s.get("score") or 0, "comments": s.get("comment_count") or 0,
                "created": created, "summary": (s.get("description") or "")[:1500],
                "author": author,
            })
    return out


# ============================================================
# DEDUPE + RANK
# ============================================================

def dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        key = (it.get("url") or it.get("title") or "").lower().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def rank(items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    for it in items:
        age_h = max((now - it["created"]).total_seconds() / 3600.0, 1)
        recency = max(1.0, 168.0 / age_h)
        eng = float(it.get("score") or 0) + float(it.get("comments") or 0) * 0.5
        # GitHub stars accumulate over years — dampen so old repos don't drown
        # this week's real signal.
        if it["source"] == "github/repo":
            eng *= 0.3
        elif it["source"].startswith("devto"):
            eng *= 1.5
        it["_rank"] = eng * recency
    items.sort(key=lambda x: -x["_rank"])
    return items


# ============================================================
# THE VET — this is where the magic lives
# ============================================================

def build_vet_system() -> str:
    """Return the system prompt for the per-item grader. The heart of it is
    stack.md — see that file for what you need to put in there."""
    if not STACK_MD_PATH.exists():
        raise FileNotFoundError(
            f"missing {STACK_MD_PATH}\n"
            "Copy stack.example.md to stack.md and edit it — this is where you\n"
            "describe your actual stack so Claude can grade candidates against\n"
            "YOUR world, not a generic one. See README.md."
        )
    your_stack = STACK_MD_PATH.read_text(encoding="utf-8").strip()

    return f"""You are the technical scout for a solo operator reviewing recent
content across the AI-agent / Claude Code / Anthropic ecosystem. Your job
is to grade each candidate against the operator's ACTUAL stack (below) and
tell them whether it's adoptable this iteration, worth watching, or just
interesting background.

=== OPERATOR'S STACK ===
{your_stack}
=== END OF STACK ===

For the candidate item you are given, return ONE JSON object with this
schema (no prose, no markdown fences):

{{
  "tag": "adopt" | "watch" | "case_study" | "prompt" | "trending",
  "adopt_score": 1-10,
  "what_it_is": "2-4 sentences — what this is, who built it, why notable",
  "use_cases": ["specific use case mapping to the operator's stack", ...] (1-3 items),
  "extracted_prompts": ["verbatim prompt or template if present", ...] (0-3 items),
  "adoption_sketch": "1-3 sentences: concrete integration plan",
  "gotchas": ["specific risks for the operator's stack", ...] (1-3 items),
  "case_study_angle": "1-2 sentences IF real metrics/outcomes; else null",
  "lessons_to_steal": "1-3 sentences: design idea or prompt pattern worth borrowing (required for tag=trending)",
  "traction_signal": "1 sentence on why this is getting attention now (required for tag=trending)",
  "build_ideas": ["concrete thing to build inspired by this — name + 1-line description", ...] (1-3 items),
  "scenarios": ["2-3 sentence narrative — e.g. 'Tuesday 9am, [agent] surfaces X, then [build idea] does Y'", ...] (1-2 items)
}}

Tagging:
- "adopt" (8+): directly applicable, integrate this iteration.
- "watch" (5-7): interesting, revisit later.
- "case_study": real-world story with metrics — worth studying even if not adopting.
- "prompt": contains a verbatim useful prompt.
- "trending": community traction but not directly adoptable — set adopt_score 1-2 for pure noise, 3-5 for genuine lessons.
"""


def vet_item(item: dict, api_key: str, system_prompt: str) -> dict | None:
    user_msg = (
        f"Source: {item['source']}\n"
        f"Title: {item['title']}\n"
        f"URL: {item.get('url')}\n"
        f"Score/comments: {item.get('score')} / {item.get('comments')}\n"
        f"Posted: {item['created'].isoformat()}\n\n"
        f"Body / description:\n{item.get('summary','')}\n\n"
        "Return ONLY the JSON object described in the system prompt. No prose."
    )
    try:
        raw = call_claude(api_key, system_prompt, user_msg, max_tokens=2200)
    except Exception as e:
        event_log(f"vet failed for {item['title'][:40]!r}: {type(e).__name__}: {e}")
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        event_log(f"vet JSON parse failed for {item['title'][:40]!r}: {e}")
        return None


# ============================================================
# RENDER
# ============================================================

def render_markdown(items: list[dict], vets: list[dict | None],
                    now: datetime, days: int) -> str:
    parts = [
        f"# Scout — week of {now.strftime('%b %d %Y')}",
        "",
        f"_Pulled {len(items)} relevant items from Reddit, GitHub, HN, dev.to, "
        f"Lobsters over the last {days} days. Each scored individually by Claude "
        f"against the operator's stack (see stack.md for the prompt context)._",
        "",
    ]

    def render_item(item: dict, vet: dict, lite: bool = False) -> list[str]:
        lines = [
            f"### {item['title']}",
            f"_{item['source']} · score {item.get('score')} · "
            f"{(datetime.now(timezone.utc) - item['created']).days}d ago · "
            f"vet: {vet.get('adopt_score','?')}/10 · [link]({item.get('url')})_",
            "",
            f"**What it is:** {vet.get('what_it_is','—')}",
            "",
        ]
        if lite:
            if vet.get("traction_signal"):
                lines += [f"**Why it's getting traction:** {vet['traction_signal']}", ""]
            if vet.get("lessons_to_steal"):
                lines += [f"**Lessons to steal:** {vet['lessons_to_steal']}", ""]
        else:
            if vet.get("use_cases"):
                lines.append("**Use cases:**")
                lines += [f"- {uc}" for uc in vet["use_cases"]] + [""]
            if vet.get("extracted_prompts"):
                lines.append("**Extracted prompts:**")
                for p in vet["extracted_prompts"]:
                    lines += ["```", p[:1000], "```"]
                lines.append("")
            if vet.get("adoption_sketch"):
                lines += [f"**Adoption sketch:** {vet['adoption_sketch']}", ""]
        if vet.get("build_ideas"):
            lines.append("**Build ideas:**")
            lines += [f"- {b}" for b in vet["build_ideas"]] + [""]
        if vet.get("scenarios"):
            lines.append("**Scenarios:**")
            lines += [f"- _{s}_" for s in vet["scenarios"]] + [""]
        if not lite and vet.get("gotchas"):
            lines.append("**Gotchas:**")
            lines += [f"- {g}" for g in vet["gotchas"]] + [""]
        if not lite and vet.get("case_study_angle"):
            lines += [f"**Case-study angle:** {vet['case_study_angle']}", ""]
        return lines

    grouped: dict[str, list[tuple[dict, dict]]] = {}
    for it, v in zip(items, vets):
        if not v:
            continue
        grouped.setdefault((v.get("tag") or "trending").lower(), []).append((it, v))

    section_order = [
        ("adopt", "ADOPT — integrate this iteration"),
        ("case_study", "CASE STUDIES — real-world stories"),
        ("prompt", "PROMPTS — verbatim worth trying"),
        ("watch", "WATCH — re-evaluate later"),
        ("trending", "TRENDING — cool community work"),
    ]
    for tag, header in section_order:
        bucket = grouped.get(tag) or []
        if not bucket:
            continue
        bucket.sort(key=lambda pair: -(pair[1].get("adopt_score") or 0))
        parts += [f"## {header} ({len(bucket)})", ""]
        for it, v in bucket:
            parts += render_item(it, v, lite=(tag == "trending"))

    unvetted = [it for it, v in zip(items, vets) if not v]
    if unvetted:
        parts += [f"## Unvetted ({len(unvetted)}) — Claude failed to rate", ""]
        parts += [f"- {it['source']} :: {it['title']} — {it.get('url')}" for it in unvetted]

    return "\n".join(parts)


def slack_summary(items: list[dict], vets: list[dict | None], now: datetime) -> str:
    counts: dict[str, int] = {"adopt": 0, "case_study": 0, "prompt": 0, "watch": 0, "trending": 0}
    top: dict[str, list[tuple[dict, dict]]] = {k: [] for k in counts}
    for it, v in zip(items, vets):
        if not v:
            continue
        tag = (v.get("tag") or "trending").lower()
        if tag in counts:
            counts[tag] += 1
            top[tag].append((it, v))
    lines = [
        f"*Scout — {now.strftime('%a %b %d')}*  ({len(items)} items: "
        f"{counts['adopt']} adopt · {counts['case_study']} case · "
        f"{counts['prompt']} prompt · {counts['watch']} watch · {counts['trending']} trending)"
    ]
    for tag, label in (("adopt", "ADOPT"), ("case_study", "CASE"),
                       ("prompt", "PROMPT"), ("trending", "TRENDING")):
        bucket = sorted(top[tag], key=lambda p: -(p[1].get("adopt_score") or 0))
        if not bucket:
            continue
        lines.append(f"\n*{label} ({len(bucket)})*")
        for it, v in bucket[:4]:
            lines.append(f"• {it['title'][:90]}")
            uc = (v.get('use_cases') or [""])[0]
            if uc:
                lines.append(f"  _use:_ {uc[:140]}")
    lines.append("\n_Full newsletter mailed._")
    return "\n".join(lines)


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--catchup", action="store_true",
                    help="180-day sweep, up to 40 items, engagement-weighted.")
    args = ap.parse_args()
    if args.catchup:
        args.days = 180
        if args.max == 20:
            args.max = 40

    load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        event_log("FATAL missing ANTHROPIC_API_KEY (in .env or environment)")
        return 1

    try:
        system_prompt = build_vet_system()
    except FileNotFoundError as e:
        event_log(f"FATAL {e}")
        return 1

    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    slack_channel = os.environ.get("SLACK_CHANNEL")  # e.g. "#scout" or "C01ABCDE"
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_pw = os.environ.get("GMAIL_APP_PASSWORD")
    email_to = os.environ.get("EMAIL_TO") or gmail_user

    now = datetime.now(timezone.utc)
    event_log(f"start days={args.days} max={args.max} catchup={args.catchup}")

    items: list[dict] = []
    items += fetch_reddit(REDDIT_SUBREDDITS, args.days, catchup=args.catchup)
    items += fetch_github(GITHUB_QUERIES, args.days)
    items += fetch_hn(HN_QUERIES, args.days)
    items += fetch_devto(DEVTO_TAGS, args.days)
    items += fetch_lobsters(args.days)
    event_log(f"fetched {len(items)} pre-dedupe")

    items = dedupe(items)
    if args.catchup:
        for it in items:
            eng = float(it.get("score") or 0) + float(it.get("comments") or 0) * 0.5
            if it["source"] == "github/repo":
                eng *= 0.3
            elif it["source"].startswith("devto"):
                eng *= 1.5
            it["_rank"] = eng
        items.sort(key=lambda x: -x["_rank"])
    else:
        items = rank(items)
    items = items[:args.max]
    event_log(f"top {len(items)} after dedupe+rank")

    hydrate_readmes(items, top_n=8)

    vets: list[dict | None] = []
    for i, item in enumerate(items):
        v = vet_item(item, api_key, system_prompt)
        vets.append(v)
        if (i + 1) % 5 == 0:
            event_log(f"vetted {i+1}/{len(items)}")
    event_log(f"vet complete; {sum(1 for v in vets if v)} succeeded")

    md = render_markdown(items, vets, now, args.days)
    slack_msg = slack_summary(items, vets, now)

    try:
        LATEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        LATEST_PATH.write_text(md, encoding="utf-8")
        event_log(f"wrote {LATEST_PATH} ({len(md)} chars)")
    except OSError as e:
        event_log(f"latest write failed: {type(e).__name__}: {e}")

    if args.dry_run:
        print("--- SLACK ---\n" + slack_msg)
        print("\n--- EMAIL MARKDOWN (first 8000 chars) ---\n" + md[:8000])
        if len(md) > 8000:
            print(f"\n[...truncated for stdout, full {len(md)} chars in file]")
        return 0

    if not items:
        event_log("no items — skipping email/slack")
        return 0

    if slack_token and slack_channel:
        try:
            post_slack(slack_token, slack_channel, slack_msg)
            event_log("slack posted")
        except Exception as e:
            event_log(f"slack post failed: {type(e).__name__}: {e}")

    if gmail_user and gmail_pw and email_to:
        try:
            send_gmail(gmail_user, gmail_pw, email_to,
                       f"Scout — {now.strftime('%b %d')}", md, as_html=True)
            event_log(f"emailed {email_to} ({len(md)} chars)")
        except Exception as e:
            event_log(f"email failed: {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
