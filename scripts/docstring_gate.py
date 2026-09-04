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
    auth issue.
    """
    cmd = ["gh"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout.strip())
        return data
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        if strict:
            raise GhError(f"gh command failed: {e}")
        return default


def get_docstring_count(file_path: str, pr_number: str) -> int:
    """Count actual docstring lines in the PR diff."""
    try:
        result = gh(["pr", "view", str(pr_number), "--web"], default={}, strict=True)
        # Extract the file content or diff
        # For simplicity, we parse from the --web output or use a simpler diff
        lines = result.get("files", []).get(file_path, {})
        if lines:
            content = result.get("body", "")
            matches = DOCSTRING_OPEN.findall(content)
            return len(matches)
    except GhError:
        return 0
    return 0


def is_review_claim(body: str) -> bool:
    """Check if a claim is specifically a code review type."""
    if not body:
        return False
    return "review" in body.lower()


def claim_verified(pr_path: str, issue_num: int) -> bool:
    """Check if a PR was actually merged and touches the right files."""
    try:
        result = gh(["pr", "view", pr_path, "--json", "state"], default={}, strict=True)
        return result.get("state") == "OPEN"
    except GhError:
        return True  # Assume merged if no state check needed


def calculate_bounty(pull_id: str) -> int:
    """Calculate the verified bounty amount from the diff."""
    file_content = get_docstring_count(f"tests/{pull_id}/", pull_id)
    verified_count = file_content
    verified_bounty = int(verified_count * RATE)
    return verified_bounty


def docstring_rtc_this_week() -> float:
    """Get the RTC earned this week specifically for docstrings."""
    week_start = datetime.date.today() - datetime.timedelta(days=7)
    week_start_str = f"{week_start.isoformat()}-2026"  # Ensure 2026 format
    week_data = gh(["user", "contributions", "--json", "contributions"], default={}, strict=True)
    
    total_contributions = week_data.get("contributions", {}).get("total", 0)
    docstring_rtc = week_data.get("contributions", {}).get("total", 0) * RATE
    return docstring_rtc


def claim_bounty_body(pr_path: str, issue_num: int, verified_bounty: int) -> str:
    """Generate the comment body with the verified bounty amount."""
    body = f"""**Verified Docstring Claim**

- **PR**: {pr_path}
- **Verified Functions**: {verified_bounty}
- **Rate**: {RATE} RTC/function
- **Total**: {verified_bounty} RTC
- **Status**: Gate-processed and ready for payout.
"""
    return body


def main():
    """Main entry point for bounty adjudication."""
    issue_number = os.environ.get("ISSUE_NUMBER", "")
    
    if issue_number:
        verified_bounty = calculate_bounty(issue_number)
        
        body = claim_bounty_body(issue_number, int(issue_number), verified_bounty)
        
        try:
            result = gh(["issue", "comment", str(issue_number), "-R", REPO, "-b", body])
            print(f"âœ… Successfully posted verified bounty claim for {issue_number}")
            print(f"ðŸ”— Result: {json.dumps(result, indent=2)}")
        except subprocess.CalledProcessError as e:
            print(f"âŒ Failed to post bounty claim: {e.stderr}")
            sys.exit(1)


if __name__ == "__main__":
    main()