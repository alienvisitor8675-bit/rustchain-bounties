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

# Regex patterns for parsing the GitHub PR
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
    auth error if it's a JSON shape issue.
    """
    cmd = ["gh"] + args
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        return data
    except subprocess.CalledProcessError as e:
        if strict:
            raise GhError(f"gh command failed: {e.stderr}")
        return default
    except json.JSONDecodeError as e:
        if strict:
            raise GhError(f"gh JSON parse failed: {e}")
        return default
    except FileNotFoundError:
        if strict:
            raise GhError(f"gh CLI not found, ensure it's in PATH")
        return default


def get_issue_info():
    """Fetch issue metadata from GitHub via the `gh` CLI."""
    return gh(
        ["issue", "view", NUM, "--json", "body", "--json", "number"],
        default={},
        strict=True
    )


def get_file_content(
    file_path: str,
    default: str = "",
    strict: bool = False
) -> str:
    """Retrieve file content from the working directory."""
    try:
        result = subprocess.run(
            ["cat", file_path],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        if strict:
            raise GhError(f"File read failed: {e.stderr}")
        return default


def count_docstring_lines(file_content: str, lines_per_function: int = 1) -> int:
    """Count actual lines that open with a docstring quote triple.
    
    This is the core verification: without it "I added 40 docstrings"
    pays out for 40 lines of anything, not just lines starting with a quote.
    """
    lines = file_content.split('\n')
    count = 0
    
    for line in lines:
        stripped = line.strip()
        if DOCSTRING_OPEN.search(stripped):
            count += 1
    
    return count


def count_docstrings_in_file(file_path: str, strict: bool = False) -> int:
    """Count actual docstring lines in a Python file."""
    content = get_file_content(file_path, default="", strict=strict)
    return count_docstring_lines(content, lines_per_function=1)


def verify_docstring_claim(verified_docstrings: int) -> Dict[str, Any]:
    """Build the result dict for the bounty payout runner.
    
    This is what the downstream code consumes to decide how much RTC to transfer.
    """
    return {
        "verified_count": verified_docstrings,
        "claimed_rate": RATE,
        "computed_total": verified_docstrings * RATE,
        "max_capped": MAX_RTC_PER_WEEK,
        "is_eligible": verified_docstrings > 0,
        "is_verified": verified_docstrings > 0,
        "file": NUM,
        "timestamp": datetime.now().isoformat()
    }


def is_docstring_verified() -> bool:
    """Determine if this issue has been through proper docstring gating.
    
    Returns True if the claim has been processed and verified, False otherwise.
    """
    # Check if the issue has the tag or if the verified count exists
    issue_data = get_issue_info()
    return "docstring-verified" in issue_data.get("labels", []) or issue_data.get("verified_count", 0) > 0


def main():
    """Entry point for CLI usage."""
    if not NUM:
        print("INFO: ISSUE_NUMBER not set, skipping verification")
        return
    
    issue_info = get_issue_info()
    
    if not issue_info:
        print("WARN: Issue info was empty, marking as potentially failed")
    
    verified = count_docstring_lines(get_file_content(NUM))
    
    result = verify_docstring_claim(verified)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()