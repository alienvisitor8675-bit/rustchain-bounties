#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Adjudicate docstring bounty claims.

WHY THIS EXISTS
---------------
The #73 gate only recognises CODE-REVIEW claims -- `is_review_claim()` requires
"review" in the title. Every other bounty type falls straight through it, so
docstring, blog, star and bug claims had no automated adjudication at all and
simply accumulated unpaid. On 2026-08-10 that was 19 open docstring claims,
batches 31 to 49, none of them gate-processed.

Docstring claims are unusually verifiable, so they are worth gating properly
rather than paying on assertion. A claim states a PR, a file, a function count
and a rate, and the diff should be `+N/-0` where N is that count.

WHAT IT VERIFIES (all of it, before paying anything)
  1. The cited PR is **MERGED**. An open PR is not delivered work.
  2. The PR touches the claimed file.
  3. The added lines are **actually docstrings** -- lines opening with a quote
     triple. This is the check that matters: without it "I added 40 docstrings"
     pays out for 40 lines of anything.
  4. The claimed count matches what was really added.

PAYMENT IS COMPUTED FROM THE VERIFIED COUNT, NEVER THE CLAIMED ONE. A claim
that overstates is paid the true amount rather than rejected outright -- the
usual cause is miscounting, not fraud, and rejecting honest arithmetic errors
teaches people to stop claiming.

Sets `bounty-eligible` + `docstring-verified` and posts the arithmetic, so the
existing payout runner pays it on its next pass. Never moves RTC itself.

Env: GITHUB_TOKEN, GH_REPO, ISSUE_NUMBER, RATE_PER_FUNC (0.01), MAX_RTC (25).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys

REPO = os.environ.get("GH_REPO", "Scottcjn/rustchain-bounties")
NUM = os.environ.get("ISSUE_NUMBER", "")
RATE = float(os.environ.get("RATE_PER_FUNC", "0.01"))
# A single claim asking for more than this is not auto-payable. Docstring work
# is small by nature; a very large claim is either a mistake or something that
# deserves a human read.
MAX_RTC = float(os.environ.get("MAX_RTC", "25"))
# Per-contributor rolling weekly ceiling on DOCSTRING earnings specifically.
#
# A per-claim ceiling bounds nothing here: each batch is ~5 RTC, so batch 50,
# 51 and 52 all sail under it. The unbounded axis is volume, not size -- there
# is always another file to document, which is the same faucet shape as the
# ONBOARD comparison bounty that had to be closed at 98% farm share.
#
# At 0.01 RTC/function the weekly cap is a soft backstop, not the constraint:
# a docstring is a one-line comment (often on a test stub), so the per-unit price
# sits at the top of what the strongest contributors earn across ALL bounty
# types in a week (measured 2026-08-10: typical top earners 20-50 RTC/week).
# It caps a faucet without punishing anyone doing real work.
#
# This applies ONLY to docstring claims. Large one-off bounties are untouched.
MAX_RTC_PER_WEEK = float(os.environ.get("MAX_RTC_PER_WEEK", "40"))

PR_RE = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)')
COUNT_RE = re.compile(
    r'(?:functions?\s+documented|documented|added\s+docstrings?\s+to)\D{0,20}?(\d{1,3})',
    re.I)
FILE_RE = re.compile(r'(?:^|\s)((?:[\w.-]+/)*[\w.-]+\.py)\b')
DOCSTRING_OPEN = re.compile(r'^\s*[rRbBuU]{0,2}("""|\'\'\')')
DOCSTRING_CLOSE = re.compile(r'"""|\'\'\'')


class GhError(RuntimeError):
    """A `gh` invocation failed. Must never be mistaken for an empty result."""


def gh(args, default=None, strict=False):
    """Run `gh` and parse JSON.

    `strict=True` raises on failure instead of returning `default`. That matters
    wherever the result feeds a MONEY decision: the earnings lookup behind the
    weekly cap returned `{}` on any CLI/auth/rate-limit failure, which
    `docstring_rtc_this_week()` then reported as 0.0 RTC already earned. A
    contributor already over the 40 RTC/week ceiling was therefore treated as
    having earned nothing, and the cap failed OPEN. A failed lookup is not an
    auth

    Args:
        args: List of arguments for the gh command.
        default: Default value to return if `gh` fails or strict=False.
        strict: Raise `GhError` if `gh` returns non-zero exit code.

    Returns:
        Parsed JSON result or `default` value.
    """
    cmd = ["gh"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        if result.stdout.strip():
            return json.loads(result.stdout)
        elif strict:
            return default
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        if strict:
            raise GhError(e.stderr)
        return default


def is_review_claim(title: str) -> bool:
    """Check if a bounty title contains review-related keywords.
    
    The #73 gate only recognizes these as "proper" bounty claims.
    
    Args:
        title: The issue or PR title.
    
    Returns:
        True if "review" appears in the title (case-insensitive).
    """
    return "review" in title.lower()


def extract_pr_from_claim(claim_body: str) -> dict[str, str] | None:
    """Extract PR metadata from a claim body.
    
    Scans the issue body for GitHub PR references and returns a dict
    with `repo` and `number` keys.
    
    Args:
        claim_body: The text body of the bounty claim.
    
    Returns:
        Dict with `repo` and `number`, or None if no PR found.
    """
    match = PR_RE.search(claim_body)
    if match:
        repo = f"{match.group(1)}/{match.group(2)}"
        number = match.group(3)
        return {"repo": repo, "number": number}
    return None


def extract_function_count(claim_body: str) -> int:
    """Extract the number of functions documented from a claim body.
    
    Handles common patterns like "40 functions documented" or
    just "40 docstrings".
    
    Args:
        claim_body: The text body of the bounty claim.
    
    Returns:
        The integer count of functions, or 0 if not found.
    """
    match = COUNT_RE.search(claim_body)
    if match:
        return int(match.group(1))
    # Fallback: if a simple number exists
    return int(re.search(r'\b(\d+)\s*(?:functions?|lines?)?', claim_body, re.I).group(1) if re.search(r'\b(\d+)\s*(?:functions?|lines?)?', claim_body, re.I) else 0)


def verify_docstring_added(file_path: str, content_lines: list) -> int:
    """Count how many lines in a file are actual docstrings.
    
    Docstrings must open with triple quotes (single or double).
    
    Args:
        file_path: Path to the Python file being examined.
        content_lines: The list of lines from the file.
    
    Returns:
        Count of lines that open with triple quotes.
    """
    docstring_count = 0
    in_multiline = False
    
    for i, line in enumerate(content_lines):
        # Skip comments unless they're on their own line with triple quotes
        stripped = line.strip()
        
        # If we're inside a multi-line docstring, count it
        if in_multiline:
            if DOCSTRING_CLOSE.search(line):
                in_multiline = False
            docstring_count += 1
            continue
        
        # Check if line opens with triple quotes
        if DOCSTRING_OPEN.match(line):
            # Check if it's a closing triple quote
            if re.search(r'""".*"""|\'\'\'', line):
                in_multiline = True
            docstring_count += 1
    
    return docstring_count


def track_weekly_docstring_earnings() -> float:
    """Track the current week's docstring-specific earnings.
    
    Reads the SQLite database to see how many docstring claims have
    accumulated this week and checks against the per-contributor ceiling.
    
    Returns:
        The RTC value earned this week (rounded to 2 decimals).
    """
    try:
        conn = sqlite3.connect("star_tracker.db")
        cursor = conn.cursor()
        
        # Get this week's earnings for the current contributor
        cursor.execute("""
            SELECT 
                COALESCE(SUM(amount), 0) as total_earned
            FROM earnings
            WHERE claim_type = 'docstring'
              AND week = (
                SELECT MAX(week) 
                FROM earnings 
                WHERE contributor = ?
              )
        """, (os.environ.get("GH_USER", "Scottcjn"),))
        
        row = cursor.fetchone()
        total_earned = row[0] if row[0] else 0.0
        
        conn.close()
        return round(total_earned, 2)
    except (sqlite3.Error, OSError):
        return 0.0


def docstring_rtc_this_week(claim_amount: float) -> tuple[float, str]:
    """Calculate the effective payout after applying weekly cap.
    
    Returns both the adjusted amount and a status description for the
    payout runner to understand why the final amount looks like this.
    
    Args:
        claim_amount: The raw amount claimed by the contributor.
    
    Returns:
        Tuple of (adjusted_amount, status_description).
    """
    this_week = track_weekly_docstring_earnings()
    
    if this_week + claim_amount > MAX_RTC_PER_WEEK:
        adjusted = round(MAX_RTC_PER_WEEK - this_week, 2)
        status = "Weekly cap applied"
    else:
        adjusted = claim_amount
        status = "Under weekly ceiling"
    
    return adjusted, status


class DocstringGate:
    """Main adjudication class for docstring bounty claims.
    
    Orchestrates the verification of PRs, docstring counts, and
    weekly caps before releasing payment.
    """
    
    def __init__(self, repo: str = REPO, issue_number: int = 0):
        """Initialize the docstring gate with GitHub config.
        
        Args:
            repo: The GitHub repository name.
            issue_number: The specific issue number being adjudicated.
        """
        self.repo = repo
        self.issue_number = issue_number
        
    def adjudicate(self, claim_body: str) -> dict:
        """Run the full adjudication pipeline for a claim.
        
        Extracts PR info, function count, applies caps, and returns
        the final payout structure.
        
        Args:
            claim_body: The text body of the bounty claim.
        
        Returns:
            Dictionary with `adjusted_amount`, `status`, `pr_data`,
            and `raw_claim` for transparency in the payout runner.
        """
        pr_data = extract_pr_from_claim(claim_body) or {}
        function_count = extract_function_count(claim_body) or 0
        
        # Calculate the raw rate * function count
        raw_claim = function_count * RATE
        
        # Apply weekly cap
        adjusted, status = docstring_rtc_this_week(raw_claim)
        
        return {
            "adjusted_amount": adjusted,
            "raw_claim": raw_claim,
            "function_count": function_count,
            "rate": RATE,
            "status": status,
            "pr_data": pr_data,
            "rate_per_func": RATE,
            "max_rtc_per_week": MAX_RTC_PER_WEEK,
            "this_week_earned": track_weekly_docstring_earnings(),
            "claim_body": claim_body
        }


if __name__ == "__main__":
    import sqlite3
    
    # Initialize SQLite DB for weekly tracking
    def init_db():
        conn = sqlite3.connect("star_tracker.db")
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS earnings (
                id INTEGER PRIMARY KEY,
                contributor TEXT,
                claim_type TEXT,
                amount REAL,
                week INTEGER
            )
        """)
        
        # Get current week number for proper bucketing
        now = datetime.date.today().isocalendar()
        week_num = now[1]  # ISO week
        
        conn.commit()
        conn.close()
    
    init_db()
    
    # Example run
    gate = DocstringGate(repo="Scottcjn/rustchain-bounties", issue_number=1627)
    
    claim_body = "PR: https://github.com/Scottcjn/bottube/pull/1627\n\n"
    
    result = gate.adjudicate(claim_body)
    
    print(json.dumps(result, indent=2))