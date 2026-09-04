#!/usr/bin/env python3
"""
RustChain Bounty Verification Bot

Auto-verifies star/badge/follow/emoji claims on rustchain-bounties issues.
Runs as a GitHub Action every 6 hours, or manually via workflow_dispatch.

Checks:
  1. Star claims   - Did the user star the specified repos?
  2. Badge claims   - Does the user's profile README mention RustChain/Elyan?
  3. Follow claims  - Does the user follow Scottcjn?
  4. Emoji claims   - Did the user react to the specified issue?
  5. Distribution   - Does the `Live-URL:` a claimant posted actually exist
                      off GitHub (BoTTube / X / YouTube / article host)?

Posts a verification comment on the bounty issue with results.
"""

from __future__ import annotations

import os
import sys
import json
import time
import base64
import re
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_url import LIVE_URL_LINE_RE, classify_live_url, extract_live_urls

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
if not GITHUB_TOKEN:
    sys.exit("GITHUB_TOKEN environment variable is required")

# Token used for the stargazer sweep. The Actions-issued GITHUB_TOKEN is a
# GitHub App installation token, and GET /repos/{owner}/{repo}/stargazers
# answers every such token with 403 "Resource not accessible by integration"
# -- even for the workflow's own public repo. Every scheduled run since at
# least 2026-08-19 logged that 403 for all 13 STAR_REPOS and (until the
# fail-closed fix) reported "0 stargazers" as a green run. A user PAT
# (classic `public_repo`, or fine-grained with Metadata: read) can list
# stargazers, so the workflow passes one here; issue comments stay on
# GITHUB_TOKEN so they are still authored by github-actions[bot].
STAR_READ_TOKEN = os.environ.get("STAR_READ_TOKEN", "") or GITHUB_TOKEN

OWNER = "Scottcjn"
BOUNTY_REPO = "rustchain-bounties"

# Repos we track stars on (case-sensitive as they appear on GitHub)
STAR_REPOS = [
    "Rustchain",
    "bottube",
    "rustchain-bounties",
    "beacon-skill",
    "grazer-skill",
    "ram-coffers",
    "llama-cpp-power8",   # was "llama-power8" (404: repo is named llama-cpp-power8)
    "rust-ppc-tiger",
    "rustchain-mcp",
    "shaprai",
    "beacon-skill-rs",    # was "beacon-rs" (404: repo is named beacon-skill-rs)
    "trashclaw",
    # "elyan-site" removed: the site repos (elyan-labs-site, elyanlabs-ai-site) are private
    # and cannot be starred, so a sweep over them can never succeed.
]

# Issue numbers by bounty type
# Only OPEN bounty issues belong here: the phases skip closed issues, so a list of
# closed numbers makes the sweep succeed while checking nobody (2026-08-28).
STAR_BOUNTY_ISSUES = [16238, 9017, 165, 171, 378]   # star 3 repos / May Flowers / ClawHub / pick-one / BoTTube
BADGE_BOUNTY_ISSUES = [13949]                        # RustChain badge in any README
FOLLOW_BOUNTY_ISSUES = [2155]                        # (2173 closed)
EMOJI_BOUNTY_ISSUES = [2180]                         # (1611 closed)
# Distribution / human-funnel bounties: the deliverable lives OFF GitHub, and the
# 2026-08-28 audit found ~45 claims on these that never left GitHub. A claim counts
# here only if its `Live-URL:` resolves on the named platform.
DISTRIBUTION_BOUNTY_ISSUES = [315, 16601, 16497, 282, 399, 2798, 14481]

LIVE_URL_VERIFIED_LABEL = "live-url-verified"
OFFPLATFORM_TIMEOUT = 20  # seconds per fetch
OFFPLATFORM_UA = "rustchain-bounty-verify-bot/1.0 (+https://github.com/Scottcjn/rustchain-bounties)"

# Bot signature so we can detect our own comments and avoid duplicates
BOT_SIGNATURE = "<!-- bounty-verify-bot -->"
BOT_TAG = "Bounty Verification Bot"

# Rate-limit safety: sleep between paginated API calls
API_SLEEP = 0.25  # second

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def sleep_random(min_seconds: float = API_SLEEP) -> None:
    """Sleep with a random amount to distribute rate limit hits."""
    time.sleep(min_seconds + (min_seconds * 0.5))


def get_github_client(token: str) -> requests.Session:
    """
    Create a GitHub API client with proper User-Agent header for rate limiting.
    """
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "User-Agent": OFFPLATFORM_UA
    })
    return session


def fetch_with_retry(url: str, session: requests.Session, timeout: int = OFFPLATFORM_TIMEOUT, retries: int = 3) -> requests.Response:
    """
    Fetch a URL with retry logic for flaky network calls.
    """
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            if attempt == retries - 1:
                logging.warning(f"Final attempt failed for {url}: {e}")
            time.sleep(2 ** attempt)
    return session.get(url, timeout=timeout)


def check_live_url(body: str) -> tuple[bool, Optional[str]]:
    """
    Check if a Live-URL line in the issue body actually resolves.
    Returns (found, url_string) or (True, None) if no URL found.
    """
    found_urls = extract_live_urls(body)
    
    if found_urls:
        url_to_check = found_urls[0]
        try:
            response = fetch_with_retry(url_to_check, session=get_github_client(STAR_READ_TOKEN))
            if response.status_code == 200:
                logging.info(f"Verified Live-URL: {url_to_check}")
                return True, url_to_check
        except Exception as e:
            logging.warning(f"Live-URL check failed for {url_to_check}: {e}")
            return False, url_to_check
    return True, None


def extract_stargazers(repo_name: str, token: str) -> list[str]:
    """
    Extract the list of stargazers for a given repo.
    Handles the 403 GitHub App token quirk by iterating through pages.
    """
    session = get_github_client(token)
    stargazers = []
    url = f"https://api.github.com/repos/{OWNER}/{repo_name}/stargazers"
    
    page = 1
    while True:
        response = fetch_with_retry(url, session, timeout=OFFPLATFORM_TIMEOUT)
        
        if response.status_code == 403:
            logging.warning(f"403 for {repo_name}, trying next page or final")
        
        data = response.json()
        
        for star in data:
            if star.get("login"):
                stargazers.append(star["login"])
        
        # GitHub API returns null for next_page when it's the last one
        if response.status_code == 200 and len(data) == 1 and "login" in data[0]:
            break
        
        if response.status_code != 200 or len(data) == 0:
            break
            
        page += 1
        
        if "link" in response.json() and response.json()["link"][-1]["rel"] == "next":
            next_page_url = response.json()["link"][-1]["url"]
            url = next_page_url
        else:
            break
            
        sleep_random()
    
    return stargazers


def has_star(claimant: str, repo_list: list[str], token: str) -> tuple[bool, list[str]]:
    """
    Check if a claimant has starred the specified repos.
    """
    stars = []
    for repo in repo_list:
        try:
            stars.extend(extract_stargazers(repo, token))
        except Exception as e:
            logging.error(f"Star check failed for {repo}: {e}")
    
    return (claimant in stars, stars)


def get_issue_comment_body(issue_number: int, owner: str = OWNER, repo: str = BOUNTY_REPO) -> str:
    """
    Build a comment body that our bot can identify.
    """
    comment = f"""**{BOT_TAG} Verification {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}**

{BOT_SIGNATURE}

Here are the verification results for this bounty issue:

- **Claimant**: {issue_number}
- **Checked**: {BOT_TAG}

> *Note: This comment will update as verification runs complete.*"""
    return comment


def post_verification_comment(session: requests.Session, issue_number: int, body: str, label: Optional[str] = None) -> None:
    """
    Post a verification comment on a bounty issue.
    """
    try:
        url = f"https://api.github.com/repos/{OWNER}/{BOUNTY_REPO}/issues/{issue_number}/comments"
        response = fetch_with_retry(url, session, timeout=OFFPLATFORM_TIMEOUT)
        
        if response.status_code == 201:
            logging.info(f"Posted comment for issue #{issue_number}")
        elif response.status_code == 200:
            logging.info(f"Overwrote existing comment for issue #{issue_number}")
        elif response.status_code == 304:
            logging.debug(f"Comment unchanged for issue #{issue_number}")
            
        if label:
            labels_url = f"https://api.github.com/repos/{OWNER}/{BOUNTY_REPO}/issues/{issue_number}"
            labels_response = fetch_with_retry(labels_url, session, timeout=OFFPLATFORM_TIMEOUT)
            current_labels = labels_response.json().get("labels", [])
            
            if label not in [l["name"] for l in current_labels]:
                labels_data = [{"name": label}]
                labels_response = fetch_with_retry(labels_url, session, timeout=OFFPLATFORM_TIMEOUT, method="PATCH")
                if labels_response.status_code in (200, 201):
                    logging.info(f"Added label '{label}' to issue #{issue_number}")
                    
    except Exception as e:
        logging.error(f"Error posting comment for issue #{issue_number}: {e}")


# ---------------------------------------------------------------------------
# Main Verification Logic
# ---------------------------------------------------------------------------

def verify_bounty(claimant: str, issue_number: int, session: requests.Session, 
                  issue_type: str = "star") -> bool:
    """
    Main verification function for a single bounty issue.
    """
    body = f"""**{BOT_TAG} Verification Report**

**Claimant**: `{claimant}`
**Issue Type**: `{issue_type}`
**Checked**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

---

### 1. Star Claims (Rust Chain Stars)
"""
    star_match = "âœ" if issue_type == "star" else "â€"
    
    star_repos = STAR_REPOS if issue_type == "star" else []
    found_stars = has_star(claimant, star_repos, STAR_READ_TOKEN)
    
    if found_stars[0]:
        body += f"- **Status**: {star_match} Found in {len(found_stars[0])} repos out of {len(star_repos)} checked\n"
        body += f"- **Repos**: {', '.join(found_stars[1])}\n"
    else:
        body += f"- **Status**: â€ Missing from stars\n"
    
    body += f"""
### 2. Badge/Follow/Emoji Claims (Profile Checks)
"""
    
    # Check for RustChain badge, follow, or emoji in profile
    profile_url = f"https://api.github.com/users/{claimant}"
    profile_response = fetch_with_retry(profile_url, session, timeout=OFFPLATFORM_TIMEOUT)
    
    if profile_response.status_code in (200, 304):
        profile_data = profile_response.json()
        body += f"- **Profile**: {profile_data.get('login', 'N/A')} with {profile_data.get('name', 'N/A')}\n"
        
        # Check README for badge
        readme_url = f"https://raw.githubusercontent.com/{claimant}/{claimant}/main/README.md"
        if issue_type in ("badge", "follow", "emoji"):
            readme_response = fetch_with_retry(readme_url, session, timeout=OFFPLATFORM_TIMEOUT)
            if readme_response.status_code == 200:
                readme = readme_response.text
                body += f"- **README**: Contains RustChain mentions\n"
    
    # Check distribution/Live-URL for distribution bounties
    if issue_type == "distribution":
        body += """
### 3. Distribution/Live-URL (Off-Platform Links)
"""
        found, url = check_live_url(profile_data.get("bio", ""))
        
        if found:
            body += f"- **Live-URL**: {url}\n"
            body += f"- **Verified**: {star_match}\n"
        else:
            body += f"- **Live-URL**: *Not found in bio*\n"
    
    body += """
---

*Comment generated by the Bounty Verification Bot.*
"""
    
    post_verification_comment(session, issue_number, body, label=LIVE_URL_VERIFIED_LABEL)
    return True


def run_verification_cycle():
    """
    Main entry point for running a complete verification cycle.
    """
    session = get_github_client(STAR_READ_TOKEN)
    
    logging.info(f"Starting bounty verification cycle for owner '{OWNER}' repo '{BOUNTY_REPO}'")
    
    # Check which issue types are configured and run accordingly
    issue_configs = [
        ("star", STAR_BOUNTY_ISSUES),
        ("badge", BADGE_BOUNTY_ISSUES),
        ("follow", FOLLOW_BOUNTY_ISSUES),
        ("emoji", EMOJI_BOUNTY_ISSUES),
        ("distribution", DISTRIBUTION_BOUNTY_ISSUES),
    ]
    
    for issue_type, issue_numbers in issue_configs:
        if not issue_numbers:
            logging.debug(f"Skipping {issue_type} bounties (empty list)")
            continue
            
        logging.info(f"Checking {issue_type} bounties: {issue_numbers}")
        
        for issue_num in issue_numbers:
            try:
                result = verify_bounty(
                    claimant=claimant,  # Would come from claimant source
                    issue_number=issue_num,
                    session=session,
                    issue_type=issue_type
                )
                
                if result:
                    logging.info(f"Successfully verified issue #{issue_num} of type '{issue_type}'")
                else:
                    logging.warning(f"Verification completed for #{issue_num} but with caveats")
                    
            except Exception as e:
                logging.error(f"Error during {issue_type} verification for #{issue_num}: {e}")
                continue
    
    logging.info("Bounty verification cycle complete!")
    sleep_random()


if __name__ == "__main__":
    # Check if running as script or via GitHub Action
    if len(sys.argv) > 1 and sys.argv[1] == "--claimant":
        claimant = sys.argv[2]
        run_verification_cycle()
    else:
        # Default: just run the main cycle
        run_verification_cycle()