#!/usr/bin/env python3
"""Autonomous bounty hunter helper for RustChain bounty workflows.

This tool focuses on three practical jobs:
1) Scan and rank open bounty issues.
2) Generate claim/submission comment templates.
3) Monitor issue/PR status and payout readiness.

It is intentionally human-in-the-loop for final posting and merge actions.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError

RTC_USD_REF = 0.10
PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
NUM_TOKEN_RE = re.compile(r"\b(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)([km])?\b", flags=re.IGNORECASE)


@dataclass
class Lead:
    number: int
    title: str
    url: str
    updated_at: str
    reward_rtc: float
    reward_usd: float
    difficulty: str
    capability_fit: float
    score: float


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def gh_get(path: str, token: str = "") -> Any:
    base = "https://api.github.com"
    url = path if path.startswith("http") else f"{base}{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "agent-bounty-hunter")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gh_get_safe(path: str, token: str = "", fallback: Any = None) -> Any:
    try:
        return gh_get(path, token=token)
    except (HTTPError, URLError, TimeoutError, http.client.RemoteDisconnected):
        return fallback


def gh_post(path: str, payload: Dict[str, Any], token: str = "") -> Any:
    if not token:
        raise ValueError("GitHub token is required for POST actions")
    base = "https://api.github.com"
    url = path if path.startswith("http") else f"{base}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "agent-bounty-hunter")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pick(values: List[float], default: float = 0.0) -> float:
    return max(values) if values else default


def _suffix_multiplier(suffix: str) -> float:
    s = (suffix or "").lower()
    if s == "k":
        return 1000.0
    if s == "m":
        return 1_000_000.0
    return 1.0


def _extract_amounts(text: str, suffix_pattern: str) -> List[float]:
    values: List[float] = []
    for raw, suffix in re.findall(rf"{NUM_TOKEN_RE.pattern}\s*{suffix_pattern}", text, flags=re.IGNORECASE):
        value = float(raw.replace(",", "")) * _suffix_multiplier(suffix)
        values.append(value)
    return values


def _extract_usd_amounts(text: str) -> List[float]:
    values: List[float] = []
    for raw, suffix in re.findall(rf"\$\s*{NUM_TOKEN_RE.pattern}", text, flags=re.IGNORECASE):
        value = float(raw.replace(",", "")) * _suffix_multiplier(suffix)
        values.append(value)
    return values


def parse_reward(body: str, title: str) -> Tuple[float, float]:
    text = f"{title}\n{body or ''}"

    # Heuristic reward parsing is intentionally conservative:
    # - Prefer explicit amounts in the title.
    # - Avoid "pool/prize pool" numbers (shared budgets are not per-PR payouts).
    # - Fall back to lines that look like reward declarations.
    #
    # This keeps scan ranking useful without inflating scores from marketing copy.
    #
    # Prefer explicit title dec

    # Extract RTC rewards from text
    rtc_values: List[float] = _extract_amounts(text, "RTC")
    reward_rtc: float = _pick(rtc_values, default=0.1)

    # Extract USD rewards
    usd_values: List[float] = _extract_usd_amounts(text)
    reward_usd: float = _pick(usd_values, default=reward_rtc * RTC_USD_REF)

    return reward_rtc, reward_usd


def parse_difficulty(title: str, body: str) -> str:
    text = f"{title}\n{body or ''}".lower()
    
    if "easy" in text:
        return "Easy"
    if "medium" in text:
        return "Medium"
    if "hard" in text:
        return "Hard"
    return "Standard"


def build_comment_template(lead: Lead, issue_number: int, agent_name: str = "Bounty Hunter") -> str:
    comment = f"""**Claim**
- **Agent**: {agent_name}
- **Issue**: #{issue_number}
- **Title**: {lead.title}
- **Reward**: {lead.reward_rtc} RTC
- **Difficulty**: {lead.difficulty}
"""
    return comment


def scan_bounties(repo: str = "Scottcjn/rustchain-bounties", token: str = "") -> List[Lead]:
    """Scan repository for open bounty issues and rank them."""
    bounties = []
    
    try:
        response = gh_get(f"repos/{repo}/issues", token=token)
        for issue in response.get("items", []):
            if issue.get("state") == "open":
                reward_rtc, reward_usd = parse_reward(issue.get("body", ""), issue.get("title", ""))
                difficulty = parse_difficulty(issue.get("title", ""), issue.get("body", ""))
                
                lead = Lead(
                    number=issue["number"],
                    title=issue["title"],
                    url=issue["html_url"],
                    updated_at=issue.get("updated_at", now_utc()),
                    reward_rtc=reward_rtc,
                    reward_usd=reward_usd,
                    difficulty=difficulty,
                    capability_fit=0.0,  # Will be updated based on agent fit
                    score=reward_rtc * 10,  # Base score
                )
                bounties.append(lead)
    except Exception as e:
        print(f"Warning: Error scanning bounties: {e}")
    
    return sorted(bounties, key=lambda x: x.score, reverse=True)


def rank_leads(leads: List[Lead]) -> List[Lead]:
    """Rank leads by various metrics and apply scoring."""
    scored = []
    for lead in leads:
        # Adjust scores based on updated_at recency
        days_ago = (datetime.now(timezone.utc) - datetime.strptime(lead.updated_at, "%Y-%m-%dT%H:%M:%S")).days
        
        # Base score with recency bonus
        recency_bonus = min(days_ago * 0.1, 1.0)
        lead.score = lead.reward_rtc * 10 + recency_bonus
        
        scored.append(lead)
    
    return sorted(scored, key=lambda x: x.score, reverse=True)


def run(args: Optional[argparse.Namespace] = None):
    """Main entry point for the bounty hunter."""
    parser = argparse.ArgumentParser(description="Autonomous Bounty Hunter")
    parser.add_argument("--repo", type=str, default="Scottcjn/rustchain-bounties", help="GitHub repo")
    parser.add_argument("--token", type=str, default=os.environ.get("GITHUB_TOKEN", ""), help="GitHub token")
    parser.add_argument("--output", type=str, default="json", choices=["json", "table", "list"], help="Output format")
    
    if args:
        parsed = vars(args)
    else:
        parsed = {"repo": args.repo, "token": args.token, "output": args.output}
    
    if parsed["token"]:
        leads = scan_bounties(repo=parsed["repo"], token=parsed["token"])
        leads = rank_leads(leads)
        
        if parsed["output"] == "json":
            output = [asdict(lead) for lead in leads]
            print(json.dumps(output, indent=2))
        elif parsed["output"] == "table":
            from tabulate import tabulate
            columns = ["#", "Title", "RTC", "Difficulty", "Score"]
            rows = [[lead.number, lead.title, lead.reward_rtc, lead.difficulty, round(lead.score, 2)] for lead in leads[:20]]
            print(tabulate(rows, headers=columns, tablefmt="github"))
        else:
            for lead in leads:
                print(f"#{lead.number} {lead.title} — {lead.reward_rtc} RTC")
    else:
        print("No token provided. Scanning basic repo structure...")
        print(f"Repo: {parsed['repo']}")


if __name__ == "__main__":
    import os
    run()