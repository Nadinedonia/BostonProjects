
import asyncio
import os
import re
import requests
import nest_asyncio
from datetime import datetime
from playwright.async_api import async_playwright
from google import genai
 
nest_asyncio.apply()
 
# --- Configuration ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
WEBHOOK_URL    = os.getenv("WEBHOOK_URL",    "YOUR_SLACK_WEBHOOK_URL")
 
BASE_URL = (
    "https://www.bostonplans.org/projects/development-projects"
    "?projectstatus=board+approved"
    "&sortby=boardapproval"
    "&sortdirection=DESC"
)
 
TODAY = datetime.today().date()
 
 
# ---------------------------------------------------------------
# 1. SCRAPE PAGE 1 — look for today's approvals only
# ---------------------------------------------------------------
async def check_for_todays_approvals() -> list[dict]:
    print(f"Checking for new approvals dated {TODAY.strftime('%b %d, %Y')}...")
    todays_projects = []
 
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()
        await page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)
 
        project_links = await page.locator("a[href*='/projects/development-projects/']").all()
        table_rows    = await page.locator("table tr").all()
 
        for i, link in enumerate(project_links):
            name = (await link.inner_text()).strip()
            href = await link.get_attribute("href") or ""
            if href and not href.startswith("http"):
                href = "https://www.bostonplans.org" + href
 
            approval_date     = None
            approval_date_str = "Unknown"
            project_type      = ""
 
            if i < len(table_rows):
                row_text = (await table_rows[i].inner_text()).strip()
 
                date_match = re.search(
                    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{4}",
                    row_text, re.IGNORECASE
                )
                if date_match:
                    approval_date_str = date_match.group()
                    try:
                        approval_date = datetime.strptime(approval_date_str, "%b %d %Y").date()
                    except ValueError:
                        pass
 
                type_match = re.search(
                    r"(Rental|Residential|Retail|Hotel|Office|Lab|Ownership|Research)[^\n]+",
                    row_text, re.IGNORECASE
                )
                if type_match:
                    project_type = type_match.group().strip()
 
            if approval_date == TODAY:
                todays_projects.append({
                    "name": name,
                    "url": href,
                    "project_type": project_type,
                    "approval_date_str": approval_date_str,
                })
            elif approval_date and approval_date < TODAY:
                break  # sorted newest first, no point continuing
 
        await context.close()
        await browser.close()
 
    print(f"Found {len(todays_projects)} project(s) approved today.")
    return todays_projects
 
 
# ---------------------------------------------------------------
# 2. AI ANALYSIS
# ---------------------------------------------------------------
def analyze_projects(projects: list[dict]) -> str:
    if not projects:
        return "NO_LEADS"
 
    projects_text = ""
    for i, p in enumerate(projects, 1):
        projects_text += (
            f"\n--- Project {i} ---\n"
            f"Name: {p['name']}\n"
            f"Type: {p['project_type']}\n"
            f"Board Approval Date: {p['approval_date_str']}\n"
            f"URL: {p['url']}\n"
        )
 
    client = genai.Client(api_key=GEMINI_API_KEY)
 
    prompt = f"""
You are a real estate lead generation specialist reviewing today's ({TODAY.strftime('%B %d, %Y')})
newly board-approved projects from the Boston Planning Department.
 
{projects_text}
 
TASK:
1. Filter for RESIDENTIAL, MULTI-FAMILY, or MIXED-USE RESIDENTIAL projects only.
   Discard purely commercial, office, lab, hotel, or institutional-only builds.
   The "Type" field tells you — keep anything with Residential, Rental, or mixed use.
 
2. For each qualifying project produce a clean Slack-ready summary:
 
   🏢 *[Project Name]*
   📅 *Board Approval Date:* [date]
   🏷️ *Type:* [project type]
   🔗 *Link:* [full URL]
   💡 *Why This Matters:* 1-2 sentences on the leasing/outreach opportunity
 
   Separate each project with: ---
 
3. End with: "✅ X new residential approval(s) on {TODAY.strftime('%b %d, %Y')}."
 
If no residential projects qualify, reply strictly with: NO_LEADS
"""
 
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text
 
 
# ---------------------------------------------------------------
# 3. SLACK ALERT
# ---------------------------------------------------------------
def send_slack_alert(message: str):
    if not message or "NO_LEADS" in message:
        print("No residential approvals today — no Slack alert sent.")
        return
 
    payload = {
        "text": (
            f"🚨 *NEW BOSTON RESIDENTIAL APPROVAL — {TODAY.strftime('%b %d, %Y')}* 🚨\n\n"
            + message
        )
    }
    try:
        res = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if res.status_code in [200, 204]:
            print("✅ Alert sent to Slack!")
        else:
            print(f"Slack error: {res.status_code} — {res.text}")
    except Exception as e:
        print(f"Failed to post to Slack: {e}")
 
 
# ---------------------------------------------------------------
# 4. MAIN
# ---------------------------------------------------------------
async def main():
    print("=" * 55)
    print(f"  Boston Daily Approval Check — {TODAY.strftime('%b %d, %Y')}")
    print("=" * 55)
 
    todays_projects = await check_for_todays_approvals()
 
    if not todays_projects:
        print("No new approvals today. Exiting quietly.")
        return
 
    print(f"\nAnalyzing {len(todays_projects)} new approval(s) with Gemini...")
    ai_analysis = analyze_projects(todays_projects)
    print("\n--- Gemini Output ---")
    print(ai_analysis)
 
    send_slack_alert(ai_analysis)
    print("\nDone.")
 
 
asyncio.run(main())
 