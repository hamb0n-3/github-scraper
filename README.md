# github-scraper

Searches GitHub for keywords or regex across code, issues, PRs, and commits, and
prints matches with a few lines of surrounding context. Handles pagination and
rate limits. Useful for finding leaked secrets or references across an org.

Needs a GitHub token (`GITHUB_TOKEN`, or `--token`).

## Usage

```
export GITHUB_TOKEN=ghp_xxx

# keywords across an org, python only
python github-scraper.py -o someorg -l python -k password secret api_key

# regex over code, more context, dump JSON
python github-scraper.py -k 'AKIA[0-9A-Z]{16}' -r --context-lines 3 --save-json hits.json
```

`-k` is required. Other flags: `-o/--organization`, `-l/--language`, `-r/--regex`,
`--categories code issues prs commits`, `--include-forks`, `--max-per-category`.
