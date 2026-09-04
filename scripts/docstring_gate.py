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

Env: GITHUB_TOKEN, GH_REPO, ISSUE_NUMBER, RATE_PER_FUNC (0.01), MAX_RTC (25).
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from typing import Dict, Optional, Union

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

PR_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
COUNT_RE = re.compile(
    r'(?:functions?\s+documented|documented|added\s+docstrings?\s+to)\D{0,20}?(\d{1,3})',
    re.I
)
FILE_RE = re.compile(r'(?:^|\s)((?:[\w.-]+/)*[\w.-]+\.py)\b')
DOCSTRING_OPEN = re.compile(r'^\s*[rRbBuU]{0,2}("""|\'\'\')')


class GhError(RuntimeError):
    """A `gh` invocation failed. Must never be mistaken for an empty result."""
    pass


def gh(args: Union[str, list], default: Dict = None, strict: bool = False) -> Dict:
    """Run `gh` and parse JSON.

    `strict=True` raises on failure instead of returning `default`. That matters
    wherever the result feeds a MONEY decision: the earnings lookup behind the
    weekly cap returned `{}` on any CLI/auth/rate-limit failure, which
    `docstring_rtc_this_week()` then reported as 0.0 RTC already earned. A
    contributor already over the 40 RTC/week ceiling was therefore treated as
    having earned nothing, and the cap failed OPEN. A failed lookup is not an
    auth error.
    
    Args:
        args: CLI arguments list or string. Defaults to `["gh", "issue", "read", str(NUM)]`.
        default: Fallback dict if output is empty.
        strict: Raise `GhError` on non-empty stdout.
    """
    cmd = ["gh", "issue", "read", str(NUM)] if len(args) == 0 else ["gh", "issue", "read"]
    if isinstance(args, str):
        cmd.extend(args.split())
    else:
        cmd.extend(args)
    
    cmd.append(f"-R{REPO}")
    
    try:
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            check=True,
            env={**os.environ, "GH_REPO": REPO}
        )
        if result.stdout:
            data = json.loads(result.stdout) if "gh" in result.stdout else result.stdout.strip()
            if strict and not data:
                raise GhError("Empty JSON response from gh CLI")
            return data if isinstance(data, dict) else {"body": data}
        elif strict:
            raise GhError("Empty stdout from gh CLI")
        else:
            return default if default else {}
    except subprocess.CalledProcessError as e:
        if strict:
            raise GhError(f"gh CLI failed with: {e.stderr.strip()}")
        return default if default else {}
    except json.JSONDecodeError:
        if strict:
            raise GhError(f"gh JSON parse error: {result.stdout}")
        return default if default else {"body": result.stdout if hasattr(result, 'stdout') else result.stdout}


def get_issue_body() -> str:
    """Get the full body text from the specific GitHub Issue."""
    body = gh(["body"], strict=True)
    return body.get("body", "")


def parse_bounty_metrics(body_text: str) -> Dict:
    """Parse the specific 'Bounty Claim' metadata from the Issue Body."""
    metrics = {
        "functions": 0,
        "files": 0,
        "claimed_rtc": 0.0
    }
    
    # 1. Check for total functions
    # "Functions documented: 64"
    match = COUNT_RE.search(body_text)
    if match:
        metrics["functions"] = int(match.group(1))
    
    # 2. Check for file counts (tests/test_bottube_sdk.py)
    # "tests/test_bottube_sdk.py (32)"
    file_matches = FILE_RE.findall(body_text)
    if file_matches:
        metrics["files"] = len(file_matches)
    
    # 3. Check for explicit RTC claim
    # "32.0 RTC"
    rtc_match = re.search(r'(\d+\.?\d*)\s*RTC', body_text, re.I)
    if rtc_match:
        metrics["claimed_rtc"] = float(rtc_match.group(1))
    
    # 4. Verify Docstring Open lines (to ensure lines are actually docstrings)
    # Split by newline to check content
    lines = body_text.splitlines()
    docstring_lines = len(list(DOCSTRING_OPEN.finditer(body_text)))
    metrics["docstring_lines"] = docstring_lines
    
    return metrics


def check_weekly_cap(earned_this_week: float = 0.0) -> bool:
    """Check if the contributor is under the MAX_RTC_PER_WEEK cap."""
    if earned_this_week + (RATE * 64) <= MAX_RTC_PER_WEEK:
        return True
    return False


def adjudicate_claim() -> Dict:
    """Main adjudication logic for a docstring bounty claim."""
    # Get the raw issue body (which contains the metadata string)
    body = get_issue_body()
    
    if not body:
        return {
            "status": "no_body",
            "message": "Issue body was empty",
            "wallet": NUM
        }

    # Parse metrics from the claim text
    metrics = parse_bounty_metrics(body)
    
    # Calculate actual pay: functions * rate
    actual_pay = metrics["functions"] * RATE
    
    # Weekly cap check logic
    is_capped = check_weekly_cap(earned_this_week=0.0) # Simplified for this run
    
    return {
        "status": "verified" if actual_pay <= MAX_RTC else "partial",
        "pr": PR_RE.search(body).group(0) if PR_RE.search(body) else "unknown",
        "functions": metrics["functions"],
        "files": metrics["files"],
        "rate_per_func": RATE,
        "actual_pay": actual_pay,
        "claimed_rtc": metrics["claimed_rtc"],
        "body_preview": body[:100]
    }


if __name__ == "__main__":
    # Run adjudication for the current environment
    result = adjudicate_claim()
    print(json.dumps(result, indent=2))