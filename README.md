# tumblr-export

Export posts and media from any public Tumblr blog to **Markdown** and **JSON** using Tumblr API v2. Zero external Python dependencies required.

## Installation

### Global CLI with `pipx` (recommended)

```bash
pipx install -e .
```

Or install directly from GitHub:

```bash
pipx install git+https://github.com/Manueloso/tumblr-export.git
```

### Configuration

1. Register a free app at [tumblr.com/oauth/apps](https://www.tumblr.com/oauth/apps) to get an **OAuth Consumer Key**.
2. Set `TUMBLR_API_KEY` in one of:
   - `.env` in your current directory
   - `~/.config/tumblr-export/.env`
   - Environment variable: `export TUMBLR_API_KEY="your_key"`

## Usage

```bash
# Export a blog by name (defaults to <name>.tumblr.com)
tumblr-export staff

# Export using full URL and download all images locally
tumblr-export https://someblog.tumblr.com --download-media

# Export only original posts (no reblogs) with a specific tag
tumblr-export someblog.tumblr.com --no-reblogs --tag fashion

# Quick test: export the 20 most recent posts only
tumblr-export staff --limit 20
```

## Output

Files are saved under `exports/<blog-domain>/` (or `-o <dir>`):
- `posts.json` — Full raw API response metadata + posts
- `markdown/` — One `.md` file per post with YAML frontmatter (`id`, `date`, `type`, `url`, `tags`)
- `media/` — Downloaded images (when `--download-media` is used)
