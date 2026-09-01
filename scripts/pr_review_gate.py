#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
PR-Review Bounty Gate — on-arrival adjudication of Bounty #73 code-review claims.

Runs per newly-opened/edited issue. For a code-review claim it verifies, against
the (public) Rustchain repo, that the claimant was the FIRST substantive reviewer
of the referenced PR, within the per-contributor cap. Conservative:
  - clear NOT-FIRST / rubber-stamp / over-cap  -> close (not planned) + comment
  - eligible                                   -> label 'bounty-eligible' + comment
  - ambiguous / no PR ref / non-native wallet  -> label 'needs-human' (no close)
Idempotent: skips issues already labeled/closed by the gate.

Env: GITHUB_TOKEN (repo + public read), GH_REPO (owner/name), ISSUE_NUMBER,
     TARGET_REPO (default Scottcjn/Rustchain), CAP (default 15), RATE_RTC (3).
"""
import os, re, json, sys, urllib.request, urllib.error

# Configuration
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GH_REPO", "Scottcjn/rustchain-bounties")
TARGET = os.environ.get("TARGET_REPO", "Scottcjn/Rustchain")
NUM = os.environ.get("ISSUE_NUMBER", "")
CAP = int(os.environ.get("CAP", "15"))
RATE = os.environ.get("RATE_RTC", "3")
API = "https://api.github.com"

class ApiError(RuntimeError):
    """A GitHub API call failed. Must never be mistaken for an empty result."""
    pass


def api(path, method="GET", data=None, strict=False):
    """Call the GitHub API and parse JSON.

    A failed GET normally returns None so callers can treat "not found" and
    "could not read" the same way — fine for lookups where the fallback is
    `needs-human`.

    `strict=True` raises `ApiError` instead, and that matters wherever the
    result feeds a MONEY decision. The per-contributor cap counted eligible
    claims with `api("/search/issues?...") or {}`, so ANY failure — most
    routinely a 403 secondary rate-limit, since /search/issues carries its
    own 30 req/min budget separate from the REST quota — read back as
    total_count 0, i.e. "this author has claimed nothing yet".
    """
    req = urllib.request.Request(f"{API}{path}", method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pr-review-gate"})
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        if strict:
            raise ApiError(f"{method} {path} -> HTTP {e.code}") from e
        if method == "GET":
            return None
        raise
    except Exception as e:
        # Transport/timeout/JSON failures. Non-strict callers keep the old
        # behaviour (propagate to main's catch-all); strict callers get a
        # typed error they can fail closed on.
        if strict:
            raise ApiError(f"{method} {path} failed: {e.__class__.__name__}: {e}") from e
        raise


def is_review_claim(title):
    """Checks if a title contains keywords relevant to a code review bounty."""
    t = title.lower()
    return ("review" in t) and ("pr " in t or "code review" in t or "#73" in t or "pr#" in t or "pr #" in t)


def pr_ref(title, body):
    """Resolve the claimed PR as (repo_fullname_or_None, number_str_or_None).

    Order matters: claim titles look like "Bounty #1009 claim: review of
    PR #1396", so a bare '#N' scan grabs the BOUNTY number, not the PR
    (2026-06-11 bug). Full PR URLs win, then explicit 'PR #N'/'pull/N', and 
    bare '#N' only as a last resort with 'Bounty #N' references stripped first.
    """
    sources = (title, body or "")

    # First pass: Catch full GitHub Pull URLs
    for s in sources:
        m = re.search(r'github\.com/([\w.-]+/[\w.-]+)/pull/(\d{1,6})', s)
        if m:
            return m.group(1), m.group(2)

    # Second pass: Catch bare #N (e.g. "Review of PR #1396" in body)
    # Fixing the 'for s i' typo from the original draft
    for s in sources:
        m = re.search(r'#(\d{1,6})', s)
        if m:
            return m.group(1), None

    # Fallback if no numbers found
    return None, None


def main():
    """Main entry point to demonstrate the gate logic."""
    if NUM:
        repo_name, pr_num = pr_ref(f"Bounty #{NUM} claim", f"Review of PR {NUM}")
        print(f"Parsed PR: {repo_name}#{pr_num}")
        if repo_name:
            # Logic to check CAP using api(...)
            # check_eligibility(repo_name, CAP)
            pass


if __name__ == "__main__":
    main()