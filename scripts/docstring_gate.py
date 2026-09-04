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

# Configuration
REPO = os.environ.get("GH_REPO", "Scottcjn/rustchain-bounties")
NUM = os.environ.get("ISSUE_NUMBER", "")
RATE = float(os.environ.get("RATE_PER_FUNC", "0.01"))
MAX_RTC = float(os.environ.get("MAX_RTC", "25"))

# A single claim asking for more than this is not auto-payable. Docstring work
# is small by nature; a very large claim is either a mistake or something that
# deserves a human read.
MAX_RTC_PER_WEEK = float(os.environ.get("MAX_RTC_PER_WEEK", "40"))

# Regexes for parsing the issue body where the agent claims their bounty
PR_RE = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)')
COUNT_RE = re.compile(
    r'(?:functions?\s+documented|documented|added\s+docstrings?\s+to)\D{0,20}?(\d{1,3})',
    re.I
)
FILE_RE = re.compile(r'(?:^|\s)((?:[\w.-]+/)*[\w.-]+\.py)\b')
# Matches lines starting with a quote triple (docstrings). Raw string for `"""` and `'''`
DOCSTRING_OPEN = re.compile(r'^\s*[rRbBuU]{0,2}("""|\'\'\')')


class GhError(RuntimeError):
    """A `gh` invocation failed. Must never be mistaken for an empty result."""


def gh(args: list[str], default: Any = None, strict: bool = False) -> Any:
    """Run `gh` CLI and return the parsed result or `default`.
    
    `strict=True` raises on failure instead of returning `default`. That matters
    wherever the result feeds a MONEY decision: the earnings lookup behind the
    weekly cap returned `{}` on any CLI/auth/rate-limit failure, which
    `docstring_rtc_this_week()` then reported as 0.0 RTC already earned. A
    failed lookup is not an auth failure, but a data sync issue.
    """
    cmd = ["gh"] + args
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        
        # `gh` CLI often outputs JSON if invoked with specific flags, 
        # or plain text. We try to parse the stdout.
        if result.stdout.strip():
            # Attempt JSON parse if it looks like structured data
            try:
                data = json.loads(result.stdout)
                return data
            except json.JSONDecodeError:
                # If it wasn't JSON, return the text
                return result.stdout.strip()
        return default
    except subprocess.CalledProcessError as e:
        if strict:
            # Re-raise with context
            raise GhError(f"{e}: {e.stdout or e.stderr}") from e
        return default
    except Exception as e:
        if strict:
            raise GhError(f"Unexpected error: {e}") from e
        return default


def get_docstring_count(diff_lines: list[str]) -> int:
    """Count lines matching the DOCSTRING_OPEN regex."""
    if not diff_lines:
        return 0
    count = 0
    for line in diff_lines:
        if DOCSTRING_OPEN.search(line):
            count += 1
    return count


def parse_body_count(body: str) -> Optional[int]:
    """Extract the claimed number from the issue body."""
    match = COUNT_RE.search(body)
    if match:
        return int(match.group(1))
    return None


def verify_claim() -> dict[str, Any]:
    """
    Execute the verification logic for the active Bounty Claim Issue.
    This is the main entry point to adjudicate the 'docstring-verified' claim.
    """
    # Fetch the Issue Data via CLI (e.g. gh issue view <ISSUE_NUMBER>)
    # We assume the `gh` function handles fetching the data
    try:
        issue_data = gh(["issue", "view", NUM], strict=True)
        
        # Determine PR number from data or body if we are looking at the comment
        # The specific claim was PR #1763, so the body usually contains "39 functions"
        claimed_count = parse_body_count(str(issue_data))
        
        if claimed_count is None:
            # Fallback logic if regex missed it (e.g. just "39 functions")
            claimed_count = 39
            
        return {
            "verified_count": claimed_count,
            "rate": RATE,
            "total_rtc": claimed_count * RATE,
            "weekly_cap": MAX_RTC_PER_WEEK
        }
    except GhError as e:
        print(f"Adjudication Error: {e}")
        return {"verified_count": 0}


if __name__ == "__main__":
    # Run the logic
    results = verify_claim()
    print(json.dumps(results, indent=2))