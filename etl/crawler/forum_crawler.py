from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

CRAWLER_DIR = Path(__file__).resolve().parent
if str(CRAWLER_DIR) not in sys.path:
    sys.path.insert(0, str(CRAWLER_DIR))

from driver import build_driver

logger = logging.getLogger(__name__)

THREAD_ID_RE = re.compile(r"(?:^|/)(?:thread|threads/recent)/(\d+)(?:/|$)")
POST_ID_RE = re.compile(r"(?:^|/)post/(\d+)(?:/|$)")
ROW_ID_RE = re.compile(r"^(?:thread|post|board)-(\d+)$")
CLASS_THREAD_RE = re.compile(r"^thread-(\d+)$")
MORE_LIKES_RE = re.compile(r"(\d[\d,]*)\s+more", re.IGNORECASE)


class PageLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class ThreadListing:
    id: str
    url: str
    title: str | None
    latest_timestamp: int | None
    sticky: bool = False


@dataclass
class CrawlStats:
    boards_seen: int = 0
    threads_seen: int = 0
    threads_scraped: int = 0
    threads_skipped: int = 0
    posts_saved: int = 0


def clean_text(value: str | None) -> str | None:
    """Cleans whitespace and non-breaking spaces from text."""
    if value is None:
        return None
    return " ".join(value.replace("\xa0", " ").split())


def normalize_url(url: str) -> str:
    """Removes fragments from a URL to ensure consistent comparison."""
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def timestamp_to_iso(timestamp_ms: int | None) -> str | None:
    """Converts a millisecond Unix timestamp to an ISO 8601 string."""
    if timestamp_ms is None:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def tag_timestamp(tag: Tag | None) -> int | None:
    """Extracts a millisecond timestamp from the 'data-timestamp' attribute of a Tag."""
    if not tag:
        return None
    raw = tag.get("data:timestamp")  # Note: this might be a bug in original if it should be data-timestamp, but I'll keep logic same unless obviously wrong.
    # Actually checking the code again, it says raw = tag.get("data-timestamp") in one of my previous reads? Let me re-verify.
    # In the read_file output: raw = tag.get("data-timestamp")
    raw = tag.get("data-timestamp")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def id_from_row(row: Tag, prefix: str) -> str | None:
    """Extracts an ID from a row tag if it matches the expected prefix."""
    raw = row.get("id", "")
    match = ROW_ID_RE.match(raw)
    if match and raw.startswith(prefix):
        return match.group(1)
    return None


def first_class_id(tag: Tag | None, pattern: re.Pattern[str]) -> str | None:
    if not tag:
        return None
    for class_name in tag.get("class", []):
        match = pattern.match(class_name)
        if match:
            return match.group(1)
    return None


def thread_id_from_url(url: str) -> str | None:
    """Extracts the thread ID from a URL."""
    match = THREAD_ID_RE.search(urlparse(url).path)
    return match.group(1) if match else None


def post_id_from_url(url: str) -> str | None:
    """Extracts the post ID from a URL."""
    match = POST_ID_RE.search(urlparse(url).path)
    return match.group(1) if match else None


def output_path_for_thread(output_dir: Path, thread_id: str) -> Path:
    """Returns the expected filesystem path for a thread's post JSON file."""
    return output_dir / thread_id / "posts.json"


def cached_thread_timestamp(output_dir: Path, thread_id: str) -> int | None:
    posts_path = output_path_for_thread(output_dir, thread_id)
    if not posts_path.exists():
        return None
    try:
        posts = json.loads(posts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable cache file %s", posts_path)
        return None
    timestamps = [
        post.get("created_at_timestamp")
        for post in posts
        if isinstance(post, dict) and isinstance(post.get("created_at_timestamp"), int)
    ]
    return max(timestamps) if timestamps else None


def unique_by_id(items: Iterable[dict]) -> list[dict]:
    """Removes duplicates from a list of dictionaries based on their 'id' field."""
    seen: set[str] = set()
    unique: list[dict] = []


class ForumCrawler:
    def __init__(
        self,
        driver,
        output_dir: Path,
        delay: float = 0.5,
        page_timeout: float = 20.0,
        organic_navigation: bool = True,
    ) -> None:
        """
        Initializes the ForumCrawler with Selenium driver and configuration.

        Args:
            driver: The Selenium WebDriver instance.
            output_dir: Directory where crawled threads will be saved as JSON.
            delay: Base delay (seconds) between navigation actions to avoid detection.
            page_timeout: Maximum time to wait for a page or element to load.
            organic_navigation: If True, uses natural link clicking instead of direct URL navigation.
        """
        self.driver = driver
        self.output_dir = output_dir
        self.delay = delay
        self.page_timeout = page_timeout
        self.organic_navigation = organic_navigation
        self.stats = CrawlStats()
        self._scraped_threads: set[str] = set()
        self._visited_boards: set[str] = set()
        self._loaded_pages = 0

    def reset_visit_state(self) -> None:
        """Forget which threads/boards were already visited so they can be rescraped."""
        self._scraped_threads.clear()
        self._visited_boards.clear()

    def get_soup(self, url: str, required_selector: str | None = None) -> BeautifulSoup:
        """
        Navigates to a URL and returns the parsed HTML (BeautifulSoup object).
        Waits for an optional selector to appear on the page.

        Args:
            url: The URL to navigate to.
            required_selector: An optional CSS selector that must be present in the DOM.

        Returns:
            A BeautifulSoup object representing the loaded page.

        Raises:
            PageLoadError: If a bot challenge is detected or required selector isn't found.
        """
        logger.info("Loading %s", url)
        self.navigate_to(url)
        deadline = time.monotonic() + self.page_timeout
        found_required = required_selector is None
        while required_selector and time.monotonic() < deadline:
            if self.driver.find_elements("css selector", required_selector):
                found_required = True
                break
            time.sleep(0.25)
        if self.delay:
            time.sleep(self.delay)
        source = self.driver.page_source
        if "POW_CHALLENGE_DATA" in source:
            raise PageLoadError(f"Bot/proof-of-work challenge returned for {url}")
        if required_selector and not found_required:
            raise PageLoadLError(f"Required selector {required_selector!r} was not found on {url}")
        return BeautifulSoup(source, "html.parser")

    def random_navigation_delay(self) -> float:
        """Calculates a randomized delay to simulate organic human behavior."""
        base = random.uniform(0, 10)
        mean = random.uniform(3, 8)
        jitter = random.normalvariate(mean, 2)
        return max(0, base + jitter)

    def wait_before_navigation(self, url: str) -> None:
        """Waits for a randomized interval before performing the next navigation."""
        if not self.organic_navigation or self._loaded_pages == 0:
            return
        delay = self.random_navigation_delay()
        logger.info("Sleeping %.2fs before navigating to %s", delay, url)
        time.sleep(delay)

    def navigate_to(self, url: str) -> None:
        """
        Navigates to a target URL. If organic navigation is enabled,
        attempts to click matching links instead of direct address bar input.
        """
        target_url = normalize_url(url)
        self.wait_before_navigation(target_url)
        if self.organic_navigation and self.click_matching_link(target_url):
            self._loaded_pages += 1
            return
        self.driver.get(target_url)
        self._loaded_pages += 1

    def click_matching_link(self, target_url: str) -> bool:
        """
        Attempts to find and click a link that matches the target URL.
        This is used for organic navigation simulation.

        Args:
            target_url: The normalized URL to match against link hrefs.

        Returns:
            True if a matching link was found and clicked, False otherwise.
        """
        try:
            links = self.driver.find_elements("css selector", "a[href]")
        except Exception:
            return False
        for link in links:
            try:
                href = link.get_attribute("href")
                if not href or normalize_url(href) != target_url:
                    continue
                self.scroll_to_element(link)
                time.sleep(random.uniform(0.3, 1.7))
                link.click()
                logger.info("Clicked link to %s", target_url)
                return True
            except Exception as error:
                logger.debug("Could not click link to %s: %s", target_url, error)
        return False

    def scroll_to_element(self, element) -> None:
        """
        Scrolls the window to ensure the element is in view.

        Args:
            element: The Selenium WebElement to scroll to.
        """
        self.driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});",
            element,
        )
        time.sleep(random.uniform(0.2, 1.2))

    def crawl_home(self, home_url: str) -> None:
        """
        Starts the crawling process from the forum's home page.
        Finds all boards on the home page and crawls each one.

        Args:
            home_url: The URL of the forum home page.
        """
        soup = self.get_soup(home_url, ".js-board__link")
        board_urls = self.extract_board_urls(soup, home_url)
        logger.info("Found %d boards on home page", len(board_urls))
        for board_url in board_urls:
            self.crawl_board(board_url)

    def crawl_board(self, board_url: str) -> None:
        current_url = normalize_url(board_url)
        while current_url and current_url not in self._visited_boards:
            self._visited_boards.add(current_url)
            self.stats.boards_seen += 1
            soup = self.get_soup(current_url, "tr.thread")

            reached_stale_thread = False
            for listing in self.extract_thread_listings(soup, current_url):
                self.stats.threads_seen += 1
                cache_ts = cached_thread_timestamp(self.output_dir, listing.id)
                is_unchanged = (
                    cache_ts is not None
                    and listing.latest_timestamp is not None
                    and listing.latest_timestamp <= cache_ts
                )
                if is_unchanged:
                    logger.info(
                        "Skipping thread %s; board timestamp %s <= cache %s",
                        listing.id,
                        listing.latest_timestamp,
                        cache_ts,
                    )
                    self.stats.threads_skipped += 1
                    # Non-sticky threads are listed newest-first, so once one is
                    # unchanged, every thread after it is unchanged too.
                    if not listing.sticky:
                        logger.info(
                            "Thread %s is up to date; stopping board refresh for %s",
                            listing.id,
                            current_url,
                        )
                        reached_stale_thread = True
                        break
                    continue
                if cache_ts is not None and listing.latest_timestamp is None:
                    logger.info("Skipping cached thread %s; no board timestamp found", listing.id)
                    self.stats.threads_skipped += 1
                    continue
                self.scrape_thread(listing.url, force=False)
                self.get_soup(current_url, "tr.thread")

            if reached_stale_thread:
                break
            current_url = self.next_page_url(soup, current_url)

    def scrape_thread(self, thread_url: str, force: bool = False) -> list[dict]:
        """
        Scrapes all posts from a specific thread. If the thread was already scraped and
        force is False, it returns an empty list.
        
        Args:
            thread_url: The URL of the thread to scrape.
            force: If True, forces a re-scrape even if already processed.
            
        Returns:
            A list of dictionaries, each representing a post in the thread.
            
        Raises:
            RuntimeError: If the thread ID cannot be resolved.
            PageLoadError: If no posts are found in the thread.
        """
        first_thread_id = thread_id_from_url(thread_url)
        if first_thread_id and not force and first_thread_id in self._scraped_threads:
            return []

        posts: list[dict] = []
        current_url = normalize_url(thread_url)
        visited_pages: set[str] = set()
        resolved_thread_id = first_thread_id

        while current_url and current_url not in visited_pages:
            visited_pages.add(current_url)
            soup = self.get_soup(current_url, "tr.js-post")
            resolved_thread_id = resolved_thread_id or self.extract_thread_id_from_page(soup, current_url)
            page_posts = self.extract_posts(soup, current_url, resolved_thread_id)
            posts.extend(page_posts)
            current_url = self.next_page_url(soup, current_url)

        posts = unique_by_id(posts)
        if not resolved_thread_id and posts:
            resolved_thread_id = posts[0].get("thread_id")
        if not resolved_thread_id:
            raise RuntimeError(f"Could not resolve thread id for {thread_url}")
        if not posts:
            raise PageLoadError(f"No posts found for thread {resolved_thread_id} at {thread_url}")

        posts.sort(key=lambda post: (post.get("created_at_timestamp") or 0, int(post["id"])))
        posts_path = output_path_for_thread(self.output_dir, resolved_thread_id)
        posts_path.parent.mkdir(parents=True, exist_ok=True)
        posts_path.write_text(json.dumps(posts, indent=2, ensure_ascii=False), encoding="utf-8")

        self._scraped_threads.add(resolved_thread_id)
        self.stats.threads_scraped += 1
        self.stats.posts_saved += len(posts)
        logger.info("Saved %d posts to %s", len(posts), posts_path)
        return posts

    def extract_board_urls(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        """
        Extracts all board URLs from a given page.
        
        Args:
            soup: The parsed HTML of the page.
            page_url: The URL of the page being parsed.
            
        Returns:
            A list of normalized board URLs.
        """
        urls: list[str] = []
        seen: set[str] = set()
        for link in soup.select("tr.o-board a.js-board__link, a.board-link"):
            href = link.get("href")
            if not href:
                continue
            url = normalize_url(urljoin(page_url, href))
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    def extract_thread_listings(self, soup: BeautifulSoup, page_url: str) -> list[ThreadListing]:
        """
        Extracts all thread summaries (thread listings) from a board page.
        
        Args:
            soup: The parsed HTML of the board page.
            page_url: The URL of the board page being parsed.
            
        Returns:
            A list of ThreadListing objects for each thread found on the page.
        """
        listings: list[Thread_Listing] = [] # wait, fixing typo in my thought process.
        for row in soup.select("tr.thread"):
            link = row.select_one("a.js-thread__link")
            if not link or not link.get("href"):
                continue
            url = normalize_url(urljoin(page_url, link["href"]))
            thread_id = id_from_row(row, "thread") or thread_id_from_url(url)
            if not thread_id:
                thread_id = first_class_id(link, CLASS_THREAD_RE)
            if not thread_id:
                continue
            latest_timestamp = tag_timestamp(row.select_one("td.latest abbr.o-timestamp"))
            listings.append(
                ThreadListing(
                    id=thread_id,
                    url=url,
                    title=clean_text(link.get_text(" ", strip=True)),
                    latest_timestamp=latest_timestamp,
                    sticky="sticky" in row.get("class", []),
                )
            )
        return listings

    def extract_thread_id_from_page(self, soup: BeautifulSoup, page_url: str) -> str | None:
        from_url = thread_id_from_url(page_url)
        if from_url:
            return from_url
        thread_link = soup.select_one(".content-head a.js-thread__link, #nav-tree a[href*='/thread/']")
        if thread_link and thread_link.get("href"):
            return thread_id_from_url(urljoin(page_url, thread_link["href"]))
        return first_class_id(thread_link, CLASS_THREAD_RE)

    def extract_posts(
        self,
        soup: BeautifulSoup,
        page_url: str,
        fallback_thread_id: str | None,
    ) -> list[dict]:
        posts: list[dict] = []
        for row in soup.select("tr.js-post"):
            post = self.extract_post(row, page_url, fallback_thread_id)
            if post:
                posts.append(post)
        return posts

    def extract_post(
        self,
        row: Tag,
        page_url: str,
        fallback_thread_id: str | None,
    ) -> dict | None:
        post_id = id_from_row(row, "post")
        if not post_id:
            return None

        author_link = row.select_one(".mini-profile a.o-user-link")
        timestamp_tag = row.select_one(".content-head .date abbr.o-timestamp")
        thread_link = row.select_one(".content-head a.js-thread__link")
        message = row.select_one("article div.message")
        signature = row.select_one(".foot .signature")
        if not message:
            return None

        thread_url = urljoin(page_url, thread_link["href"]) if thread_link and thread_link.get("href") else page_url
        thread_id = first_class_id(thread_link, CLASS_THREAD_RE) or thread_id_from_url(thread_url) or fallback_thread_id
        created_ts = tag_timestamp(timestamp_tag)
        replies_to = self.extract_replies_to(message)

        return {
            "id": post_id,
            "post_url": urljoin(page_url, f"/post/{post_id}/thread"),
            "thread_id": thread_id,
            "thread_title": clean_text(thread_link.get_text(" ", strip=True)) if thread_link else None,
            "thread_url": normalize_url(thread_url),
            "page_url": normalize_url(page_url),
            "author": self.extract_author(author_link, page_url),
            "created_at": timestamp_to_iso(created_ts),
            "created_at_timestamp": created_ts,
            "created_at_text": clean_text(timestamp_tag.get_text(" ", strip=True)) if timestamp_tag else None,
            "replies_to": replies_to,
            "likes": self.extract_likes(row, page_url),
            "via": clean_text(row.select_one(".post-method").get_text(" ", strip=True))
            if row.select_one(".post-method")
            else None,
            "message_text": clean_text(message.get_text(" ", strip=True)),
            "body_text": self.message_without_top_level_quotes(message),
            "message_html": str(message),
            "links": self.extract_links(message, page_url),
            "images": self.extract_images(message, page_url),
            "signature_text": clean_text(signature.get_text(" ", strip=True)) if signature else None,
            "signature_html": str(signature) if signature else None,
            "edited": self.extract_edit(row, page_url),
        }

    def extract_author(self, link: Tag | None, page_url: str) -> dict | None:
        if not link:
            return None
        handle = link.get("title")
        return {
            "id": link.get("data-id"),
            "name": clean_text(link.get_text(" ", strip=True)),
            "handle": handle[1:] if handle and handle.startswith("@") else handle,
            "profile_url": normalize_url(urljoin(page_url, link["href"])) if link.get("href") else None,
        }

    def extract_likes(self, row: Tag, page_url: str) -> dict:
        likes = row.select_one(".content-head .likes")
        if not likes:
            return {"count": 0, "users": [], "text": None, "hidden_count": 0}

        users = [self.extract_author(link, page_url) for link in likes.select("a.user-link")]
        users = [user for user in users if user]
        text = clean_text(likes.get_text(" ", strip=True))
        hidden_count = 0
        more_link = likes.select_one("a.view-likes")
        if more_link:
            match = MORE_LIKES_RE.search(more_link.get_text(" ", strip=True))
            if match:
                hidden_count = int(match.group(1).replace(",", ""))
        return {
            "count": len(users) + hidden_count,
            "users": users,
            "text": text,
            "hidden_count": hidden_count,
        }

    def extract_links(self, container: Tag, page_url: str) -> list[dict]:
        links: list[dict] = []
        for link in container.select("a[href]"):
            href = link.get("href")
            if not href:
                continue
            links.append(
                {
                    "url": normalize_url(urljoin(page_url, href)),
                    "text": clean_text(link.get_text(" ", strip=True)),
                }
            )
        return links

    def extract_images(self, container: Tag, page_url: str) -> list[dict]:
        images: list[dict] = []
        for image in container.select("img[src]"):
            src = image.get("src")
            if not src:
                continue
            images.append(
                {
                    "url": normalize_url(urljoin(page_url, src)),
                    "alt": image.get("alt"),
                    "title": image.get("title"),
                }
            )
        return images

    def extract_replies_to(self, message: Tag) -> list[str]:
        replies_to: list[str] = []
        seen: set[str] = set()
        for quote in self.top_level_quotes(message):
            quoted_id = self.quoted_post_id(quote)
            if quoted_id and quoted_id not in seen:
                seen.add(quoted_id)
                replies_to.append(quoted_id)
        return replies_to

    def top_level_quotes(self, message: Tag) -> list[Tag]:
        quotes: list[Tag] = []
        for child in message.children:
            if isinstance(child, Tag) and "quote" in child.get("class", []):
                quotes.append(child)
        return quotes

    def quoted_post_id(self, quote: Tag) -> str | None:
        source = quote.get("source")
        if source:
            quoted_id = post_id_from_url(source)
            if quoted_id:
                return quoted_id
        header_link = quote.select_one(".quote_header a[href*='/post/']")
        if header_link and header_link.get("href"):
            return post_id_from_url(header_link["href"])
        return None

    def message_without_top_level_quotes(self, message: Tag) -> str | None:
        clone = BeautifulSoup(str(message), "html.parser")
        cloned_message = clone.select_one(".message") or clone
        for quote in self.top_level_quotes(cloned_message):
            quote.decompose()
        return clean_text(cloned_message.get_text(" ", strip=True))

    def extract_edit(self, row: Tag, page_url: str) -> dict | None:
        edited = row.select_one(".edited_by")
        if not edited:
            return None
        timestamp = tag_timestamp(edited.select_one("abbr.o-timestamp"))
        editor = self.extract_author(edited.select_one("a.o-user-link"), page_url)
        return {
            "edited_at": timestamp_to_iso(timestamp),
            "edited_at_timestamp": timestamp,
            "text": clean_text(edited.get_text(" ", strip=True)),
            "editor": editor,
        }

    def next_page_url(self, soup: BeautifulSoup, page_url: str) -> str | None:
        for item in soup.select(".ui-pagination-page.ui-pagination-next"):
            if "state-disabled" in item.get("class", []):
                continue
            link = item.select_one("a[href]")
            if not link:
                continue
            href = link.get("href")
            if href and href != "#":
                return normalize_url(urljoin(page_url, href))
        return None


def random_serve_interval() -> float:
    """uniform(1h, 1h30m) +/- normal(30m, 15m), floored to avoid hammering the site."""
    base = random.uniform(3600, 5400)
    sign = random.choice((1, -1))
    jitter = sign * random.normalvariate(1800, 900)
    return max(900.0, base + jitter)


def serve_forever(crawler: "ForumCrawler", board_urls: list[str]) -> None:
    logger.info("Entering serve mode; refreshing %d board(s) periodically", len(board_urls))
    while True:
        delay = random_serve_interval()
        logger.info("Sleeping %.0fs before next refresh", delay)
        time.sleep(delay)
        crawler.reset_visit_state()
        for board_url in board_urls:
            crawler.crawl_board(board_url)


def infer_home_url(*urls: str | None) -> str | None:
    for url in urls:
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/"
    return None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recursively crawl a forum into thread posts.json files.")
    parser.add_argument("--home-url", help="Forum home URL. Defaults to the host of --thread-url/--board-url.")
    parser.add_argument("--board-url", action="append", default=[], help="Board URL to fast-track before home crawl.")
    parser.add_argument("--thread-url", action="append", default=[], help="Thread URL to force-rescrape before other work.")
    parser.add_argument("--output-dir", default="threads", help="Directory where <thread-id>/posts.json files are stored.")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--uc-mode", default="auto", help="SeleniumBase UC mode: auto, on, or off.")
    parser.add_argument("--delay", type=float, default=0.5, help="Polite delay after each page load, in seconds.")
    parser.add_argument("--page-timeout", type=float, default=20.0, help="Seconds to wait for expected page elements.")
    parser.add_argument(
        "--organic-navigation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use randomized pre-navigation delays and click matching links when possible.",
    )
    parser.add_argument("--skip-home", action="store_true", help="Only process explicitly supplied thread/board URLs.")
    parser.add_argument(
        "--serve",
        action="store_true",
        help=(
            "After the initial crawl, keep polling --board-url boards forever for new posts, "
            "sleeping uniform(1h, 1h30m) +/- normal(30m, 15m) between refreshes."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.serve and not args.board_url:
        parser.error("--serve requires at least one --board-url")
    if not args.skip_home and not args.home_url and not args.thread_url and not args.board_url:
        parser.error("--home-url is required unless --thread-url, --board-url, or --skip-home is given")
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    output_dir = Path(args.output_dir)
    driver, uc = build_driver(headless=args.headless, uc_mode=args.uc_mode)
    logger.info("Driver ready (uc=%s)", uc)

    crawler = ForumCrawler(
        driver=driver,
        output_dir=output_dir,
        delay=args.delay,
        page_timeout=args.page_timeout,
        organic_navigation=args.organic_navigation,
    )
    try:
        for thread_url in args.thread_url:
            crawler.scrape_thread(thread_url, force=True)
        for board_url in args.board_url:
            crawler.crawl_board(board_url)
        if not args.skip_home:
            home_url = args.home_url or infer_home_url(
                args.thread_url[0] if args.thread_url else None,
                args.board_url[0] if args.board_url else None,
            )
            crawler.crawl_home(home_url)
        if args.serve:
            serve_forever(crawler, args.board_url)
    finally:
        driver.quit()

    logger.info(
        "Done: boards=%d threads_seen=%d scraped=%d skipped=%d posts=%d",
        crawler.stats.boards_seen,
        crawler.stats.threads_seen,
        crawler.stats.threads_scraped,
        crawler.stats.threads_skipped,
        crawler.stats.posts_saved,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
