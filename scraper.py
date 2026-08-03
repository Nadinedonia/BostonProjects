# ============================================================
# GitHub Actions Workflow
# Runs the Boston Planning scraper and commits its state file back.
#
# The scraper alerts on any project URL it has not seen before, so running
# more than once a day costs nothing and cannot double-post. The extra runs
# exist because the BPDA publishes board approvals after the meeting, often
# the following morning.
# ============================================================

name: Daily Boston Planning Check

on:
  schedule:
    # 13:00 UTC = 9:00 AM Boston (EDT). Catches anything published overnight
    # after an evening board meeting.
    - cron: "0 13 * * *"
    # 21:30 UTC = 5:30 PM Boston (EDT). Deliberately off the hour: GitHub
    # queues scheduled runs at :00 and delays are common there.
    - cron: "30 21 * * *"

  workflow_dispatch:

# Required so the job can commit seen_projects.json back to the repo.
permissions:
  contents: write

# Two runs must never overwrite each other's state file.
concurrency:
  group: boston-approval-check
  cancel-in-progress: false

jobs:
  check-approvals:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          pip install playwright google-genai requests
          playwright install --with-deps chromium

      - name: Run approval check
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          WEBHOOK_URL: ${{ secrets.WEBHOOK_URL }}
        run: python scraper.py

      # Persisting state is what makes the run stateful. Without this step
      # every run starts blind and the whole design collapses back to the
      # old "only today's date counts" behaviour.
      - name: Commit updated state
        if: always()
        run: |
          if [[ -n "$(git status --porcelain seen_projects.json)" ]]; then
            git config user.name  "github-actions[bot]"
            git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
            git add seen_projects.json
            git commit -m "chore: update seen projects [skip ci]"
            git push
          else
            echo "No state change to commit."
          fi
 
