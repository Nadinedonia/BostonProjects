"""
Boston Planning Department — new board-approved project watcher.

Design notes (why this differs from the previous version):

  * State, not "today". The old script only accepted rows where the board
    approval date equalled the day the script happened to run. The BPDA
    publishes approvals *after* the board meets, so anything with publish lag
    was missed permanently. This version remembers every project URL it has
    already reported in seen_projects.json and alerts on anything new,
    regardless of date.

  * No index pairing. The old script zipped project_links[i] to table_rows[i].
    One stray anchor shifted every date onto the wrong project. Each card is
    now parsed from its own DOM subtree, so a project's date always comes from
    that project.

  * Deterministic residential filter. Gemini is used only to write the
    "why this matters" line. If Gemini errors or is not configured, the alert
    still goes out.

  * Loud failures. Any scrape/parse problem exits non-zero so the Actions run
    goes red instead of silently reporting nothing.
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# --- Configuration -------------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

# Matches the page you actually monitor, including reviewtype=1.
BASE_URL = (
    "https://www.bostonplans.org/projects/development-projects"
    "?projectstatus=board+approved"
    "&reviewtype=1"
    "&sortby=boardapproval"
    "&sortdirection=DESC"
)

# Page 1 holds 10 rows. A busy board night can fill all of them, so read a few.
PAGES_TO_SCRAPE = int(os.getenv("PAGES_TO_SCRAPE", "3"))

STATE_FILE = Path(__file__).parent / "seen_projects.json"

# Project types worth alerting on.
RESIDENTIAL_KEYWORDS = {"residential", "rental", "ownership"}

MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
DATE_RE = re.compile(rf"\b({MONTHS})\w*\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I)


# --- State ---------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": {}, "initialized": False}
    try:
        data = json.loads(STATE_FILE.read_text())
        data.setdefault("seen", {})
        data.setdefault("initialized", True)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        # Never silently start from empty — that would re-alert on everything.
        sys.exit(f"FATAL: {STATE_FILE.name} exists but is unreadable: {exc}")


def save_state(state: dict) -> None:
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# --- Scrape --------------------------------------------------------------

# Runs in the page. For each project anchor, climb to the nearest ancestor
# that actually contains a board-approval date and return that subtree's text.
# Each project therefore carries its own date — no positional guessing.
EXTRACT_JS = r"""
() => {
  const dateRe = /\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4}\b/i;
  const out = [];
  const seen = new Set();

  const anchors = Array.from(
    document.querySelectorAll("a[href*='/projects/development-projects/']")
  );

  for (const a of anchors) {
    const href = a.getAttribute("href") || "";
    // Real project pages look like /projects/development-projects/<slug>.
    // Nav and filter links carry a query string instead.
    const m = href.match(/\/projects\/development-projects\/([^?#]+)$/);
    if (!m) continue;
    const slug = m[1].replace(/\/$/, "");
    if (!slug || seen.has(slug)) continue;

    const name = (a.innerText || a.textContent || "").trim();
    if (!name) continue;

    // Climb until the subtree holds a date, but stop before we swallow the
    // whole listing (which would hand this project someone else's date).
    let node = a;
    let block = null;
    for (let depth = 0; depth < 8 && node; depth++) {
      const text = (node.innerText || "").trim();
      if (dateRe.test(text) && text.length < 800) { block = text; break; }
      node = node.parentElement;
    }
    if (!block) continue;

    seen.add(slug);
    out.push({ slug, name, href, block });
  }
  return out;
}
"""


def parse_card(card: dict) -> dict | None:
    block = re.sub(r"[ \t]+", " ", card["block"])

    date_match = DATE_RE.search(block)
    if not date_match:
        return None
    month, day, year = date_match.groups()
    try:
        approval_date = datetime.strptime(
            f"{month[:3].title()} {day} {year}", "%b %d %Y"
        ).date()
    except ValueError:
        return None

    # The cell renders as "<types>Project Type", sandwiched after the status.
    project_type = ""
    for pattern in (
        r"Project Status\s*(.{2,80}?)\s*Project Type",
        r"([A-Za-z][A-Za-z /&,\-]{1,60}?)\s*Project Type",
    ):
        type_match = re.search(pattern, block, re.I | re.S)
        if type_match:
            project_type = " ".join(type_match.group(1).split())
            break

    href = card["href"]
    if not href.startswith("http"):
        href = "https://www.bostonplans.org" + href

    return {
        "slug": card["slug"],
        "name": card["name"],
        "url": href,
        "project_type": project_type,
        "approval_date": approval_date.isoformat(),
        "approval_date_str": approval_date.strftime("%b %d, %Y"),
    }


async def scrape_projects() -> list[dict]:
    projects: list[dict] = []
    unparsed = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        for page_num in range(1, PAGES_TO_SCRAPE + 1):
            url = BASE_URL if page_num == 1 else f"{BASE_URL}&page={page_num}"
            print(f"Fetching page {page_num}...")
            await page.goto(url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(1500)

            cards = await page.evaluate(EXTRACT_JS)
            if not cards:
                # An empty page 1 means the layout changed or we got blocked.
                # Either way it must not be mistaken for "no new projects".
                if page_num == 1:
                    await context.close()
                    await browser.close()
                    sys.exit(
                        "FATAL: found 0 project cards on page 1. The page "
                        "layout probably changed — alerts would silently stop."
                    )
                break

            for card in cards:
                parsed = parse_card(card)
                if parsed:
                    projects.append(parsed)
                else:
                    unparsed += 1
                    print(f"  WARN: could not parse a date for {card['name']!r}")

        await context.close()
        await browser.close()

    # Dedupe across pages, keeping first occurrence.
    unique: dict[str, dict] = {}
    for proj in projects:
        unique.setdefault(proj["slug"], proj)

    print(f"Parsed {len(unique)} project(s); {unparsed} unparseable.")
    return sorted(unique.values(), key=lambda p: p["approval_date"], reverse=True)


# --- Filtering -----------------------------------------------------------

def is_residential(project: dict) -> bool:
    text = project["project_type"].lower()
    return any(keyword in text for keyword in RESIDENTIAL_KEYWORDS)


# --- Optional AI colour --------------------------------------------------

def add_why_it_matters(projects: list[dict]) -> None:
    """Best-effort. Never gates whether an alert is sent."""
    if not GEMINI_API_KEY or not projects:
        return

    try:
        from google import genai

        listing = "\n".join(
            f"{i}. {p['name']} — {p['project_type']} — approved {p['approval_date_str']}"
            for i, p in enumerate(projects, 1)
        )
        prompt = (
            "You are a real estate lead generation specialist. For each "
            "newly board-approved Boston project below, write ONE sentence on "
            "the leasing or outreach opportunity it represents.\n\n"
            f"{listing}\n\n"
            "Reply with one line per project, formatted exactly as "
            "'<number>. <sentence>'. No preamble, no other text."
        )

        client = genai.Client(api_key=GEMINI_API_KEY)
        response = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt
        )

        for line in (response.text or "").splitlines():
            line_match = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
            if not line_match:
                continue
            idx = int(line_match.group(1)) - 1
            if 0 <= idx < len(projects):
                projects[idx]["why"] = line_match.group(2).strip()
    except Exception as exc:  # noqa: BLE001 — commentary is never worth failing over
        print(f"WARN: Gemini commentary unavailable ({exc}). Sending alert anyway.")


# --- Slack ---------------------------------------------------------------

def build_message(projects: list[dict]) -> str:
    count = len(projects)
    plural = "" if count == 1 else "s"
    lines = [f"🚨 *{count} NEW BOSTON RESIDENTIAL APPROVAL{plural.upper()}* 🚨", ""]

    for project in projects:
        lines += [
            f"🏢 *{project['name']}*",
            f"📅 *Board Approval Date:* {project['approval_date_str']}",
            f"🏷️ *Type:* {project['project_type'] or 'Unspecified'}",
            f"🔗 *Link:* {project['url']}",
        ]
        if project.get("why"):
            lines.append(f"💡 *Why This Matters:* {project['why']}")
        lines += ["", "---", ""]

    return "\n".join(lines).rstrip("\n- ")


def send_slack_alert(message: str) -> None:
    if not WEBHOOK_URL:
        sys.exit("FATAL: WEBHOOK_URL is not set — cannot deliver the alert.")

    response = requests.post(WEBHOOK_URL, json={"text": message}, timeout=15)
    if response.status_code not in (200, 204):
        # Exit non-zero so a broken webhook shows up as a red run rather than
        # a line of log output nobody reads.
        sys.exit(f"FATAL: Slack returned {response.status_code} — {response.text}")
    print("✅ Alert sent to Slack.")


# --- Main ----------------------------------------------------------------

async def main() -> None:
    print("=" * 60)
    print(f"  Boston board-approval check — "
          f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC")
    print("=" * 60)

    state = load_state()
    seen: dict = state["seen"]

    projects = await scrape_projects()
    new_projects = [p for p in projects if p["slug"] not in seen]

    print(f"{len(new_projects)} project(s) not seen before.")

    # First ever run: record the backlog rather than firing 30 alerts at once.
    if not state.get("initialized"):
        for project in projects:
            seen[project["slug"]] = {
                "name": project["name"],
                "approval_date": project["approval_date"],
                "alerted": False,
            }
        state["initialized"] = True
        save_state(state)
        print(f"Seeded state with {len(projects)} existing project(s). "
              "No alert sent on first run.")
        return

    if not new_projects:
        print("Nothing new. Exiting.")
        save_state(state)
        return

    residential = [p for p in new_projects if is_residential(p)]
    skipped = [p for p in new_projects if not is_residential(p)]

    for project in skipped:
        print(f"  Skipping non-residential: {project['name']} "
              f"({project['project_type'] or 'no type listed'})")

    if residential:
        add_why_it_matters(residential)
        send_slack_alert(build_message(residential))
    else:
        print("New projects found, but none residential. No alert sent.")

    # Record every new project, alerted or not, so it is never reconsidered.
    for project in new_projects:
        seen[project["slug"]] = {
            "name": project["name"],
            "approval_date": project["approval_date"],
            "alerted": project in residential,
        }
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
