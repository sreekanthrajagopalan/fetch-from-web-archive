# fetch-from-web-archive

Builds an index of the **latest Wayback Machine snapshot of every page** under a domain
(on or before a cutoff date), downloads the original page content, and packs it into a
`.tar.gz`. Runs on demand from GitHub Actions.

## Run it from GitHub

1. Go to **Actions → Fetch from Web Archive → Run workflow**.
2. Enter:
   - **domain**: e.g. `example.com`
   - **cutoff_date**: `YYYY-MM-DD` (snapshots up to and including this day)
   - optional: `match_type` (`domain` includes subdomains), `html_only`, `max_pages`
3. When the run finishes, download the **web-archive-&lt;run id&gt;** artifact (kept 14 days). It contains:
   - `<domain>_<cutoff>.tar.gz`: the pages, laid out as `host/path/...`, plus `_index.csv` and `_manifest.json`
   - `<domain>_<cutoff>_index.csv`: the index (URL, snapshot timestamp, mimetype, digest, status, errors)

Tip: do a first run with `max_pages = 25` to check the output before a full crawl.

## Run it locally

```bash
pip install -r requirements.txt
python fetch_archive.py example.com 2020-12-31 --html-only --max-pages 25
python fetch_archive.py example.com 2020-12-31 --index-only   # index only, no downloads
```

Options: `--match-type {domain,host,prefix,exact}`, `--workers N` (default 4),
`--delay SECONDS` (global min gap between requests, default 0.5), `--max-file-mb` (default 50).

## Notes

- Only HTTP 200 captures are used; for each URL the newest capture at or before the cutoff wins.
- Pages are fetched with Wayback's `id_` mode, so you get the original bytes (no Wayback toolbar or link rewriting).
- Be polite to archive.org: keep workers low. The client retries on 429/5xx with backoff.
- GitHub-hosted jobs are limited to 6 hours and artifacts to a few GB; use `html_only` / `max_pages` for very large sites.
- Only archive content you're permitted to copy; Wayback captures may be subject to copyright.
