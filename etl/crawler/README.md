# Crawler

Recursive forum crawler for saving forum posts as JSON. The crawler
uses Selenium/SeleniumBase to load pages, parses the rendered forum HTML, and
writes one cache/output file per thread:

```text
<output-dir>/<thread-id>/posts.json
```

`posts.json` is a JSON array containing every scraped post in that thread.
Downstream, the [ingestion pipeline](../ingestion/README.md) reads these files
and indexes them into OpenSearch.

## Files

- `main.py` — CLI entry point.
- `forum_crawler.py` — `ForumCrawler` class and all HTML parsing/extraction logic.
- `driver.py` — builds the Selenium/SeleniumBase Chromium driver.
- `Dockerfile` — Chromium + Xvfb image for running the crawler in a container.

## What It Crawls

The crawler supports three entry paths:

- `--thread-url`: scrape that thread immediately, page by page, ignoring any
  existing cache for that thread.
- `--board-url`: scrape that board immediately, page by page, thread by thread.
  Threads are skipped when their cached newest post is at least as recent as the
  board's latest-post timestamp.
- `--home-url`: discover every board from the forum home page and then scrape
  each board.

If `--thread-url` or `--board-url` is provided without `--skip-home`, the crawler
processes those first and then continues with the forum home crawl.

## Setup

Install dependencies with Pipenv (from the repo root):

```bash
pipenv install
```

The crawler depends on:

- `beautifulsoup4`
- `selenium`
- `seleniumbase`

`driver.py` creates the browser driver. By default it tries SeleniumBase UC mode
on non-ARM machines and falls back to regular Selenium Chromium on ARM or when
UC mode is disabled.

## Basic Usage

Scrape a full forum:

```bash
pipenv run python crawler/main.py \
  --home-url https://forum.example.com/ \
  --output-dir threads
```

Scrape one thread, ignoring its cache:

```bash
pipenv run python crawler/main.py \
  --thread-url https://forum.example.com/thread/3557/example-thread-title \
  --output-dir threads \
  --skip-home
```

Scrape one board, using thread-level cache checks:

```bash
pipenv run python crawler/main.py \
  --board-url https://forum.example.com/board/2/general-board \
  --output-dir threads \
  --skip-home
```

Show all options:

```bash
pipenv run python crawler/main.py --help
```

## CLI Options

```text
--home-url URL       Forum home URL. Defaults to the host from a supplied
                    thread or board URL. Required if neither is given
                    (unless --skip-home is set).
--board-url URL      Board URL to crawl before the home crawl. Can be repeated.
--thread-url URL     Thread URL to force-rescrape before other work. Can be
                    repeated.
--output-dir DIR     Output directory. Default: threads.
--headless           Run browser headless. Enabled by default.
--no-headless        Run browser visibly.
--uc-mode MODE       SeleniumBase UC mode: auto, on, or off. Default: auto.
--delay SECONDS      Delay after each page load. Default: 0.5.
--page-timeout SEC   Wait time for expected page elements. Default: 20.
--organic-navigation / --no-organic-navigation
                    Add randomized sleeps before page navigation and click
                    matching page links when possible. Enabled by default.
--skip-home          Only process explicitly supplied thread/board URLs.
--serve              After the initial crawl, keep polling --board-url boards
                    forever for new posts.
--log-level LEVEL    Python logging level. Default: INFO.
```

With organic navigation enabled, each navigation after the first waits for:

```text
uniform(0, 10) + normal(uniform(3, 8), 2)
```

The crawler clicks matching thread, board, and pagination links when those links
are present on the current page. It falls back to direct URL navigation when the
target URL is not reachable from the currently loaded page.

## Caching

The cache is the saved thread file itself:

```text
threads/<thread-id>/posts.json
```

When a thread is seen in a board list, the crawler reads that file and finds the
newest `created_at_timestamp` among the saved posts.

- If the board's latest-post timestamp is newer, the thread is scraped again.
- If the cache is current, the thread is skipped.
- Explicit `--thread-url` always force-rescrapes the thread.

The cache date is therefore the timestamp of the newest saved post.

## Output Format

Each post object includes fields such as:

```json
{
  "id": "25609",
  "post_url": "https://forum.example.com/post/25609/thread",
  "thread_id": "3557",
  "thread_title": "Running an RE5 with 10% Ethanol & 87-91octane",
  "thread_url": "https://forum.example.com/thread/3557/example-thread-title",
  "page_url": "https://forum.example.com/thread/3557/example-thread-title",
  "author": {
    "id": "1363",
    "name": "laurier",
    "handle": "laurier",
    "profile_url": "https://forum.example.com/user/1363"
  },
  "created_at": "2026-04-04T19:21:39+00:00",
  "created_at_timestamp": 1775330499000,
  "created_at_text": "Apr 4, 2026 at 3:21pm",
  "replies_to": ["25408"],
  "likes": {
    "count": 0,
    "users": [],
    "text": null,
    "hidden_count": 0
  },
  "via": null,
  "message_text": "...",
  "body_text": "...",
  "message_html": "<div class=\"message\">...</div>",
  "links": [],
  "images": [],
  "signature_text": null,
  "signature_html": null,
  "edited": null
}
```

`replies_to` contains only the top-level quoted post IDs in that post. Nested
quotes are ignored, but multiple top-level quoted replies are all included.

## Docker

Build the image (from the repo root, since it needs the shared `Pipfile`):

```bash
docker build -f crawler/Dockerfile -t forum-scraper .
```

Run a full crawl and write output to a local `threads/` directory:

```bash
docker run --rm \
  -v "$PWD/threads:/app/threads" \
  forum-scraper \
  pipenv run python -u crawler/main.py \
    --home-url https://forum.example.com/ \
    --output-dir /app/threads
```

The Docker image uses `selenium/standalone-chromium` and installs the
SeleniumBase driver binaries during build.

## Development Notes

`page-samples/` holds saved example forum pages (`forum-home/`, `forum-board/`,
`forum-thread/`, `forum-thread-with-replies/`) used to inspect the forum
structure while developing the parsing logic. It's gitignored — kept locally
only, not committed. The live crawler fetches pages through Selenium instead.

Run a syntax check:

```bash
python3 -m py_compile crawler/forum_crawler.py crawler/main.py crawler/driver.py
```
