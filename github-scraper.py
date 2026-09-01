#!/usr/bin/env python3
import os
import re
import json
import time
import base64
import argparse
import logging
from typing import List, Dict, Optional, Tuple, Iterable

import requests

# ──────────────────────────────────────────────────────────────────────────────
# Settings (centralized defaults; CLI can override many of these)
# ──────────────────────────────────────────────────────────────────────────────
SETTINGS = {
    "API_BASE": "https://api.github.com",
    "USER_AGENT": "GitHub-Scraper/2.0",
    "TIMEOUT": 10,                          # seconds per request
    "MAX_RETRIES": 3,                       # default retry count for API calls
    "RETRY_BACKOFF_BASE": 2,                # exponential backoff base (seconds)
    "RATE_LIMIT_PAD": 10,                   # seconds to add after rate-limit reset
    "PER_PAGE": 100,                        # GitHub maximum is typically 100
    "SEARCH_CATEGORIES": ("code", "issues", "prs", "commits"),
    "DEFAULT_LANGUAGE": "",                 # empty => all languages
    "DEFAULT_CONTEXT_LINES": 5,             # lines of context above/below a match
    "DEFAULT_MAX_PER_CATEGORY": 120,        # safety cap per category
    "INCLUDE_FORKS_CODE": False,            # add 'fork:false' for code search by default
    # Output
    "SHOW_TOP_N_PER_CATEGORY": 10,          # how many to print per category
}

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("github-scraper")


# ──────────────────────────────────────────────────────────────────────────────
# Argument parsing
# ──────────────────────────────────────────────────────────────────────────────
def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search GitHub for keywords (or regex) in code, issues, PRs, and commits."
    )
    parser.add_argument("-o", "--organization", default="",
                        help="GitHub organization name (optional)")
    parser.add_argument("-k", "--keywords", required=True, nargs="+",
                        help="Keywords (literal) or regex patterns (with -r/--regex)")
    parser.add_argument("-l", "--language", default=SETTINGS["DEFAULT_LANGUAGE"],
                        help="Programming language to search in (code search only)")
    parser.add_argument("-t", "--token", default=os.getenv("GITHUB_TOKEN"),
                        help="GitHub token (default: from GITHUB_TOKEN env var)")
    parser.add_argument("--case-sensitive", action="store_true",
                        help="Perform case-sensitive matching (local filtering)")
    parser.add_argument("--max-retries", type=int, default=SETTINGS["MAX_RETRIES"],
                        help=f"Max retries for API requests (default: {SETTINGS['MAX_RETRIES']})")
    parser.add_argument("--wildcard", action="store_true",
                        help="Append wildcard (*) to each search term for remote search (literal mode only)")
    parser.add_argument("-r", "--regex", action="store_true",
                        help="Treat -k/--keywords as regular expressions for local matching")
    parser.add_argument("--context-lines", type=int, default=SETTINGS["DEFAULT_CONTEXT_LINES"],
                        help=f"Lines of context around a match (default: {SETTINGS['DEFAULT_CONTEXT_LINES']})")
    parser.add_argument("--max-per-category", type=int, default=SETTINGS["DEFAULT_MAX_PER_CATEGORY"],
                        help=f"Max items to fetch per category (default: {SETTINGS['DEFAULT_MAX_PER_CATEGORY']})")
    parser.add_argument("--categories", nargs="+",
                        choices=list(SETTINGS["SEARCH_CATEGORIES"]),
                        default=list(SETTINGS["SEARCH_CATEGORIES"]),
                        help="Which categories to search")
    parser.add_argument("--include-forks", action="store_true",
                        help="Include forks in *code* search (default off)")
    parser.add_argument("--save-json", default="",
                        help="Optional: path to save raw results as JSON")
    return parser.parse_args()


# ──────────────────────────────────────────────────────────────────────────────
# Query construction
# ──────────────────────────────────────────────────────────────────────────────
def regex_seed_terms(pattern: str) -> List[str]:
    tokens = re.findall(r"[A-Za-z0-9_]{3,}", pattern or "")
    # Prefer longer tokens; cap to 2 per pattern to keep queries small
    tokens = sorted(set(tokens), key=len, reverse=True)
    return tokens[:2] if tokens else []


def build_remote_terms(keywords: List[str], use_regex: bool) -> List[str]:
    if not use_regex:
        return keywords
    seeds: List[str] = []
    for pat in keywords:
        seeds.extend(regex_seed_terms(pat))
    # Fallback to raw strings if nothing usable was found
    return seeds or keywords


def quote_or_wildcard(term: str, wildcard: bool) -> str:
    return f"{term}*" if wildcard else f"\"{term}\""


def create_search_query(
    terms: List[str],
    category: str,
    org: str,
    language: str,
    wildcard: bool,
    include_forks_code: bool,
) -> str:
    # Terms (AND semantics)
    term_bits = [quote_or_wildcard(t, wildcard) for t in terms] if terms else []
    q = " ".join(term_bits)

    # Qualifiers
    if org:
        q += f" org:{org}"
    if category == "code":
        # Only add fork:true when explicitly requested. The code search endpoint
        # doesn't accept fork:false and will 422 if we send it.
        if include_forks_code:
            q += " fork:true"
    elif category == "issues":
        q += " type:issue"
    elif category == "prs":
        q += " type:pr"
    elif category == "commits":
        # no extra qualifier needed; endpoint defines the type
        pass

    return q.strip() or "*"  # GitHub requires non-empty, '*' matches many things (still requires another qualifier)


# ──────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────────────
def make_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": SETTINGS["USER_AGENT"],
        "X-GitHub-Api-Version": "2022-11-28",
    }


def sleep_until_reset(response_headers: Dict[str, str]) -> None:
    remaining = int(response_headers.get("X-RateLimit-Remaining", "1") or "1")
    if remaining >= 1:
        return
    reset_ts = int(response_headers.get("X-RateLimit-Reset", str(int(time.time()) + 60)))
    sleep_for = max(reset_ts - time.time(), 0) + SETTINGS["RATE_LIMIT_PAD"]
    logger.warning(f"Rate limit exceeded. Sleeping for {sleep_for:.1f} seconds")
    time.sleep(sleep_for)


def fetch_github_paginated(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, str]] = None,
    *,
    max_retries: int,
    max_items: int,
    timeout: int,
) -> List[Dict]:
    items: List[Dict] = []
    first = True
    retries = 0
    next_url = url

    while next_url and (max_items <= 0 or len(items) < max_items):
        try:
            resp = session.get(
                next_url,
                headers=headers,
                params=params if first else None,
                timeout=timeout,
            )
            first = False
            if resp.status_code == 403:  # often rate-limited
                sleep_until_reset(resp.headers)
                continue
            try:
                resp.raise_for_status()
            except requests.exceptions.HTTPError as e:
                # Surface GitHub's error details to help diagnose (e.g., 422 with invalid qualifiers)
                detail = ""
                try:
                    detail = resp.json().get("message", "") or resp.text
                except Exception:
                    detail = resp.text
                logger.warning(f"Request failed ({resp.status_code} {resp.reason}) for {resp.url}. Details: {detail}")
                # Fall back to retry logic below
                raise

            data = resp.json()
            page_items = data.get("items", [])
            items.extend(page_items)
            if max_items > 0 and len(items) >= max_items:
                break

            # Pagination
            if "next" in resp.links:
                next_url = resp.links["next"]["url"]
            else:
                break

            retries = 0  # successful page -> reset retries

        except requests.exceptions.RequestException as e:
            retries += 1
            if retries > max_retries:
                logger.error(f"Giving up after {max_retries} retries. Last error: {e}")
                break
            backoff = (SETTINGS["RETRY_BACKOFF_BASE"] ** retries)
            logger.warning(f"Request failed ({e}). Retrying in {backoff} seconds...")
            time.sleep(backoff)

    # Trim if we went over
    return items[:max_items] if max_items > 0 else items


def fetch_code_content(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    timeout: int,
) -> Optional[str]:
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
        if resp.status_code == 403:
            sleep_until_reset(resp.headers)
            return None
        resp.raise_for_status()
        content_b64 = resp.json().get("content", "")
        return base64.b64decode(content_b64).decode("utf-8", errors="ignore")
    except (requests.exceptions.RequestException, KeyError, UnicodeDecodeError) as e:
        logger.error(f"Failed to fetch code content: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Matching & formatting
# ──────────────────────────────────────────────────────────────────────────────
def compile_patterns(patterns: Iterable[str], use_regex: bool, case_sensitive: bool) -> List[re.Pattern]:
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled: List[re.Pattern] = []
    for p in patterns:
        compiled.append(re.compile(p if use_regex else re.escape(p), flags))
    return compiled


def any_match(text: str, compiled: List[re.Pattern]) -> List[str]:
    matched: List[str] = []
    for pat in compiled:
        if pat.search(text):
            matched.append(pat.pattern)
    return matched


def extract_context(
    content: str,
    compiled: List[re.Pattern],
    *,
    context_lines: int,
) -> List[Tuple[str, str, int]]:
    results: List[Tuple[str, str, int]] = []
    lines = content.splitlines()
    for i, line in enumerate(lines):
        for pat in compiled:
            if pat.search(line):
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                block = "\n".join(lines[start:end])
                results.append((pat.pattern, block, i + 1))
                break  # one entry per line is enough
    return results


def format_output(result: Dict, category: str) -> str:
    out: List[str] = []
    if category == "code":
        out.append(f"Pattern: {result['pattern']}")
        out.append(f"Repo:    {result.get('repo','?')}")
        out.append(f"Path:    {result.get('path','?')}")
        out.append(f"Line:    {result['line']}")
        out.append(f"URL:     {result['url']}")
        out.append("Context:")
        out.append(result["context"])
    else:
        out.append(f"Matched Patterns: {', '.join(result['patterns'])}")
        out.append(f"URL:     {result['url']}")
        out.append("Content:")
        snippet = result["content"]
        out.append(snippet[:500] + ("..." if len(snippet) > 500 else ""))
    return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────────
# Main search
# ──────────────────────────────────────────────────────────────────────────────
def search_github(args: argparse.Namespace) -> Dict[str, List]:
    session = requests.Session()
    headers = make_headers(args.token)

    # Build remote search terms (regex -> derived seeds; literal -> keywords)
    remote_terms = build_remote_terms(args.keywords, use_regex=args.regex)

    # Precompile local matchers
    compiled = compile_patterns(args.keywords, use_regex=args.regex, case_sensitive=args.case_sensitive)

    results: Dict[str, List] = {}

    # Endpoints map
    endpoint_for = {
        "code":     f"{SETTINGS['API_BASE']}/search/code",
        "issues":   f"{SETTINGS['API_BASE']}/search/issues",
        "prs":      f"{SETTINGS['API_BASE']}/search/issues",
        "commits":  f"{SETTINGS['API_BASE']}/search/commits",
    }

    for category in args.categories:
        logger.info(f"Searching {category}...")
        q = create_search_query(
            remote_terms,
            category=category,
            org=args.organization,
            language=args.language,
            wildcard=args.wildcard if not args.regex else False,  # wildcards don't apply to regex mode locally
            include_forks_code=args.include_forks,
        )

        params = {"q": q, "per_page": str(SETTINGS["PER_PAGE"])}
        url = endpoint_for[category]

        items = fetch_github_paginated(
            session,
            url,
            headers,
            params=params,
            max_retries=args.max_retries,
            max_items=args.max_per_category,
            timeout=SETTINGS["TIMEOUT"],
        )

        # Per-category handling
        if category == "code":
            collected = []
            for item in items:
                content_url = item.get("url")
                html_url = item.get("html_url")
                repo = (item.get("repository") or {}).get("full_name", "")
                path = item.get("path", "")
                if not content_url:
                    continue

                content = fetch_code_content(session, content_url, headers, timeout=SETTINGS["TIMEOUT"])
                if not content:
                    continue

                for pat, ctx, line_num in extract_context(
                    content,
                    compiled,
                    context_lines=args.context_lines,
                ):
                    collected.append({
                        "pattern": pat,
                        "context": ctx,
                        "url": html_url,
                        "line": line_num,
                        "repo": repo,
                        "path": path,
                    })
            results["code"] = collected

        else:
            collected = []
            for item in items:
                if category in ("issues", "prs"):
                    # Both come from /search/issues; we distinguish by qualifier in query.
                    title = (item.get("title") or "")
                    body = (item.get("body") or "")
                    content = f"{title}\n{body}"
                    html_url = item.get("html_url")
                else:  # commits
                    commit = item.get("commit") or {}
                    content = (commit.get("message") or "")
                    html_url = item.get("html_url")

                matched = any_match(content, compiled)
                if matched:
                    collected.append({
                        "content": content,
                        "url": html_url,
                        "patterns": matched,
                    })
            results[category] = collected

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
def main():
    args = parse_arguments()
    if not args.token:
        logger.error("GitHub token required. Use --token or set GITHUB_TOKEN environment variable.")
        return

    results = search_github(args)

    # Optional: save raw results
    if args.save_json:
        try:
            with open(args.save_json, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved raw results to {args.save_json}")
        except OSError as e:
            logger.error(f"Failed to save JSON: {e}")

    # Print nicely
    for category, items in results.items():
        print(f"\n{'='*40}")
        print(f"{category.upper()} RESULTS ({len(items)} found):")
        print('='*40)
        for idx, item in enumerate(items[:SETTINGS["SHOW_TOP_N_PER_CATEGORY"]], 1):
            print(f"\nResult #{idx}:")
            print(format_output(item, category))
            print('-'*40)


if __name__ == "__main__":
    main()
