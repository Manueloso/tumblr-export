#!/usr/bin/env python3
"""CLI for exporting posts and media from any public Tumblr blog via API v2."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://api.tumblr.com/v2"
USER_AGENT = "tumblr-export/0.1.0 (+https://github.com)"


def load_dotenv(env_path: Path) -> None:
    """Load key=value pairs from a .env file into os.environ if not already set."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_env_locations() -> None:
    """Load .env from CWD, ~/.config/tumblr-export/.env, or the repo root."""
    candidates = [
        Path.cwd() / ".env",
        Path.home() / ".config" / "tumblr-export" / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for path in candidates:
        load_dotenv(path)


def normalize_blog_identifier(raw: str) -> str:
    """Normalize a blog name, hostname, or URL into a Tumblr blog identifier."""
    cleaned = raw.strip()
    if "://" in cleaned:
        parsed = urllib.parse.urlparse(cleaned)
        cleaned = parsed.netloc or parsed.path
    cleaned = cleaned.strip("/").split("/")[0]
    if "." not in cleaned:
        cleaned = f"{cleaned}.tumblr.com"
    return cleaned


def api_get(url: str, max_retries: int = 5) -> dict[str, Any]:
    """Perform a GET request against the Tumblr API with rate-limit backoff."""
    delay = 2.0
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 or 500 <= exc.code < 600:
                retry_after = exc.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                if attempt == max_retries:
                    raise RuntimeError(f"HTTP {exc.code} after {max_retries} attempts: {body}") from exc
                print(f"Rate limited / server error ({exc.code}). Waiting {wait:.1f}s...", file=sys.stderr)
                time.sleep(wait)
                delay *= 2
                continue
            raise RuntimeError(f"Tumblr API error {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            if attempt == max_retries:
                raise RuntimeError(f"Network error: {exc.reason}") from exc
            print(f"Network error ({exc.reason}). Retrying in {delay:.1f}s...", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("Unreachable")


def fetch_posts(
    blog: str,
    api_key: str,
    *,
    tag: str | None = None,
    limit: int | None = None,
    include_reblogs: bool = True,
    only_reblogs: bool = False,
    delay_seconds: float = 0.25,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch posts from a public Tumblr blog using `before` timestamp pagination."""
    posts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    blog_info: dict[str, Any] = {}
    before: int | None = None
    total_posts: int | None = None

    while True:
        params: dict[str, Any] = {
            "api_key": api_key,
            "npf": "true",
            "reblog_info": "true",
            "limit": 20,
        }
        if before is not None:
            params["before"] = before
        if tag:
            params["tag"] = tag

        query = urllib.parse.urlencode(params)
        url = f"{API_BASE}/blog/{urllib.parse.quote(blog)}/posts?{query}"
        payload = api_get(url)
        response = payload.get("response", {})

        if not blog_info and "blog" in response:
            blog_info = response["blog"]
        if total_posts is None:
            total_posts = response.get("total_posts")

        batch = response.get("posts", [])
        if not batch:
            break

        oldest_ts: int | None = None
        for post in batch:
            post_id = str(post.get("id_string") or post.get("id"))
            ts = post.get("timestamp")
            if isinstance(ts, int):
                oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)

            if post_id in seen_ids:
                continue
            seen_ids.add(post_id)

            is_reblog = bool(post.get("reblogged_from_id") or post.get("trail"))
            if not include_reblogs and is_reblog:
                continue
            if only_reblogs and not is_reblog:
                continue

            posts.append(post)
            if limit is not None and len(posts) >= limit:
                print(f"Fetched {len(posts)} posts (limit reached).")
                return blog_info, posts

        progress_total = f"/{total_posts}" if total_posts is not None and not tag else ""
        print(f"Fetched {len(posts)}{progress_total} posts...", flush=True)

        if oldest_ts is None or (before is not None and oldest_ts >= before):
            break
        before = oldest_ts

        if delay_seconds > 0:
            time.sleep(delay_seconds)

    return blog_info, posts


def apply_inline_formatting(text: str, formatting: list[dict[str, Any]] | None) -> str:
    """Apply NPF inline formatting (bold, italic, links, mentions) to text."""
    if not formatting or not text:
        return text

    result = text
    sorted_fmt = sorted(formatting, key=lambda f: (f.get("start", 0), f.get("end", 0)), reverse=True)
    for fmt in sorted_fmt:
        start = fmt.get("start", 0)
        end = fmt.get("end", 0)
        ftype = fmt.get("type")
        if start < 0 or end > len(result) or start >= end:
            continue
        snippet = result[start:end]
        if ftype == "bold":
            replacement = f"**{snippet}**"
        elif ftype == "italic":
            replacement = f"*{snippet}*"
        elif ftype == "strikethrough":
            replacement = f"~~{snippet}~~"
        elif ftype == "link" and fmt.get("url"):
            replacement = f"[{snippet}]({fmt['url']})"
        elif ftype == "mention" and fmt.get("blog", {}).get("url"):
            replacement = f"[{snippet}]({fmt['blog']['url']})"
        else:
            continue
        result = result[:start] + replacement + result[end:]
    return result


def pick_best_media(media_list: list[dict[str, Any]]) -> str | None:
    """Pick the highest-resolution media URL from an NPF media array."""
    if not media_list:
        return None
    best = max(media_list, key=lambda m: (m.get("width") or 0) * (m.get("height") or 0))
    return best.get("url")


def download_file(url: str, dest: Path) -> Path | None:
    """Download a remote media file if not already cached locally."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            dest.write_bytes(resp.read())
        return dest
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: failed to download {url}: {exc}", file=sys.stderr)
        return None


def safe_filename(name: str, max_len: int = 60) -> str:
    """Sanitize a string for safe filesystem names."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.")
    return cleaned[:max_len] or "post"


def render_npf_blocks(
    blocks: list[dict[str, Any]],
    *,
    media_dir: Path | None = None,
    post_id: str = "",
) -> list[str]:
    """Convert NPF content blocks into Markdown lines."""
    lines: list[str] = []
    img_idx = 0

    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            text = apply_inline_formatting(block.get("text", ""), block.get("formatting"))
            subtype = block.get("subtype")
            if subtype == "heading1":
                lines.append(f"# {text}")
            elif subtype == "heading2":
                lines.append(f"## {text}")
            elif subtype == "quirky":
                lines.append(f"*{text}*")
            elif subtype in ("quote", "indented"):
                lines.append(f"> {text}")
            elif subtype == "chat":
                lines.append(f"`{text}`")
            elif subtype == "ordered-list-item":
                lines.append(f"1. {text}")
            elif subtype == "unordered-list-item":
                lines.append(f"- {text}")
            else:
                lines.append(text)
            lines.append("")

        elif btype == "image":
            img_idx += 1
            url = pick_best_media(block.get("media", []))
            alt = (block.get("alt_text") or "").replace("\n", " ").strip()
            if url:
                target = url
                if media_dir is not None:
                    ext = Path(urllib.parse.urlparse(url).path).suffix or ".jpg"
                    local_file = media_dir / f"{post_id}_{img_idx}{ext}"
                    if download_file(url, local_file):
                        target = f"../media/{local_file.name}"
                lines.append(f"![{alt}]({target})")
                if block.get("caption"):
                    lines.append(f"*{block['caption']}*")
                lines.append("")

        elif btype == "link":
            title = block.get("title") or block.get("url") or "Link"
            url = block.get("url", "")
            lines.append(f"[{title}]({url})")
            if block.get("description"):
                lines.append(f"> {block['description']}")
            lines.append("")

        elif btype == "video":
            url = block.get("url") or (block.get("media") or {}).get("url")
            if url:
                lines.append(f"[Video]({url})")
                lines.append("")

        elif btype == "audio":
            url = block.get("url") or (block.get("media") or {}).get("url")
            title = block.get("title") or "Audio"
            if url:
                lines.append(f"[{title}]({url})")
                lines.append("")

        elif btype == "ask":
            asking_name = (block.get("asking_name") or "Anonymous").strip()
            lines.append(f"**{asking_name} asked:**")
            lines.append("")

    return lines


def post_to_markdown(post: dict[str, Any], media_dir: Path | None = None) -> str:
    """Render a Tumblr post (NPF) into a Markdown document with YAML frontmatter."""
    post_id = str(post.get("id_string") or post.get("id"))
    ts = post.get("timestamp", 0)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    post_url = post.get("post_url", "")
    tags = post.get("tags", [])
    post_type = post.get("type", "unknown")

    frontmatter = [
        "---",
        f'id: "{post_id}"',
        f'date: "{dt}"',
        f'type: "{post_type}"',
        f'url: "{post_url}"',
        f"tags: {json.dumps(tags, ensure_ascii=False)}",
        "---",
        "",
    ]

    body: list[str] = []
    trail = post.get("trail") or []
    for item in trail:
        blog_name = (item.get("blog") or {}).get("name") or "unknown"
        body.append(f"### {blog_name}:")
        body.append("")
        body.extend(render_npf_blocks(item.get("content", []), media_dir=media_dir, post_id=f"{post_id}_{blog_name}"))

    content = post.get("content") or []
    if content:
        if trail:
            body.append(f"### {post.get('blog_name', 'op')}:")
            body.append("")
        body.extend(render_npf_blocks(content, media_dir=media_dir, post_id=post_id))

    if tags:
        body.append("Tags: " + ", ".join(f"#{t}" for t in tags))
        body.append("")

    return "\n".join(frontmatter + body).rstrip() + "\n"


def export_blog(args: argparse.Namespace) -> None:
    load_env_locations()
    api_key = args.api_key or os.environ.get("TUMBLR_API_KEY")
    if not api_key:
        print(
            "Error: Missing Tumblr API key. Set TUMBLR_API_KEY in .env (or ~/.config/tumblr-export/.env) or pass --api-key.",
            file=sys.stderr,
        )
        sys.exit(1)

    blog = normalize_blog_identifier(args.blog)
    out_dir = Path(args.output) / safe_filename(blog, max_len=80)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Exporting blog '{blog}' into {out_dir}...")
    blog_info, posts = fetch_posts(
        blog,
        api_key,
        tag=args.tag,
        limit=args.limit,
        include_reblogs=not args.no_reblogs,
        only_reblogs=args.only_reblogs,
        delay_seconds=args.delay,
    )

    if args.format in ("json", "all"):
        json_path = out_dir / "posts.json"
        json_payload = {"blog": blog_info, "exported_at": datetime.now(timezone.utc).isoformat(), "posts": posts}
        json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved JSON: {json_path} ({len(posts)} posts)")

    if args.format in ("markdown", "all"):
        md_dir = out_dir / "markdown"
        md_dir.mkdir(parents=True, exist_ok=True)
        media_dir = (out_dir / "media") if args.download_media else None

        for post in posts:
            post_id = str(post.get("id_string") or post.get("id"))
            ts = post.get("timestamp", 0)
            date_prefix = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            slug = safe_filename(post.get("slug") or "")
            filename = f"{date_prefix}_{post_id}_{slug}.md" if slug else f"{date_prefix}_{post_id}.md"
            md_text = post_to_markdown(post, media_dir=media_dir)
            (md_dir / filename).write_text(md_text, encoding="utf-8")

        print(f"Saved Markdown files: {md_dir} ({len(posts)} files)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tumblr-export",
        description="Export posts from any public Tumblr blog.",
    )
    parser.add_argument("blog", help="Blog name, hostname, or URL (e.g. staff, staff.tumblr.com, https://staff.tumblr.com)")
    parser.add_argument("--api-key", help="Tumblr OAuth Consumer Key (defaults to TUMBLR_API_KEY env/.env)")
    parser.add_argument("-o", "--output", default="exports", help="Output directory (default: exports)")
    parser.add_argument(
        "-f",
        "--format",
        choices=("markdown", "json", "all"),
        default="all",
        help="Export format (default: all)",
    )
    parser.add_argument("--download-media", action="store_true", help="Download images into media/ and link locally in Markdown")
    parser.add_argument("--tag", help="Only export posts with this tag")
    parser.add_argument("--limit", type=int, help="Maximum number of posts to export")
    parser.add_argument("--no-reblogs", action="store_true", help="Exclude reblogged posts")
    parser.add_argument("--only-reblogs", action="store_true", help="Only export reblogged posts")
    parser.add_argument("--delay", type=float, default=0.25, help="Delay in seconds between API pages (default: 0.25)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    export_blog(parse_args(argv))


if __name__ == "__main__":
    main()

