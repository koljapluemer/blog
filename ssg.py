"""Minimal static site generator for a flat Obsidian folder.

Usage:
    uv run ssg.py

Reads ``config.json`` for the Obsidian note folder and media folder, converts
every ``*.md`` file to an HTML page using ``post.jinja``, builds an index with
``index.jinja`` (posts reverse-sorted by their ``created`` front-matter date),
copies only the media that is actually embedded, and writes the result to
``_site/`` (override with ``"outputPath"`` in the config).

Obsidian conventions handled:
    ``[[Note]]`` / ``[[Note|alias]]``      -> link to that post (plain text if unknown)
    ``![[file.png]]`` / ``![[f.png|alt]]`` -> embedded media (img/video/audio)
    ``![alt](file.png)``                   -> embedded media, ``alt`` kept as alt text
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader
from PIL import Image, ImageOps

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"

IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".apng"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v", ".ogv"}
AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus"}

# Index thumbnails are shown at 3.25rem (~52px); 3x covers the densest phone
# screens. A small square WebP keeps the index cheap even with hundreds of
# image posts, since the browser never downloads the full-size embed for it.
THUMB_PX = 156

# Matches both the embed form (leading "!") and the plain wikilink form.
WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
# Standard Markdown image: ![alt](path "optional title")
MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
LIST_ITEM_RE = re.compile(r"^[ \t]*([-*+]|\d+[.)])[ \t]+\S")


def slugify(name: str) -> str:
    """URL-safe slug from a note/heading name (keeps it readable, lowercased)."""
    s = name.strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9._-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "post"


def media_out_name(filename: str) -> str:
    """Output filename for a media asset: slugified stem + original suffix."""
    p = Path(filename)
    return slugify(p.stem) + p.suffix.lower()


def make_thumb(src: Path, out_dir: Path) -> str | None:
    """Write a small square WebP thumbnail of ``src`` into ``out_dir``.

    Returns the output filename, or ``None`` when the image can't be rasterised
    (SVG, or an unreadable/unsupported file) so the caller can fall back to
    linking the original.
    """
    if src.suffix.lower() == ".svg":
        return None
    out_name = slugify(src.stem) + "-thumb.webp"
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
            im = im.convert("RGBA" if has_alpha else "RGB")
            im = ImageOps.fit(im, (THUMB_PX, THUMB_PX), method=Image.LANCZOS)
            im.save(out_dir / out_name, "WEBP", quality=80, method=6)
    except Exception as exc:  # noqa: BLE001 - any decode failure -> caller falls back
        print(f"warning: thumbnail for {src.name} failed ({exc})", file=sys.stderr)
        return None
    return out_name


def resolve_path(value: str) -> Path:
    """Expand ``~`` and ``$VARS``; keep absolute paths as-is; resolve a relative
    path against the directory holding ``config.json`` (so it doesn't depend on
    the working directory you run from)."""
    p = Path(os.path.expandvars(os.path.expanduser(value)))
    if not p.is_absolute():
        p = CONFIG_PATH.parent / p
    return p.resolve()


def load_config() -> tuple[Path, Path, Path]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    notes = resolve_path(cfg.get("obsidianPath") or ".")
    media = resolve_path(cfg.get("obsidianMediaPath") or cfg.get("obsidianPath") or ".")
    out = resolve_path(cfg.get("outputPath") or "_site")
    return notes, media, out


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[m.end():]


def loosen_lists(text: str) -> str:
    """Obsidian (like CommonMark) lets a list start on the line directly below a
    paragraph; python-markdown needs a blank line there, so add one."""
    out: list[str] = []
    in_fence = False
    prev = ""
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        if (
            not in_fence
            and prev.strip()
            and not LIST_ITEM_RE.match(prev)
            and not prev.lstrip().startswith(("#", ">"))
            and LIST_ITEM_RE.match(line)
        ):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def parse_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


class Renderer:
    """Resolves Obsidian links/embeds against the known notes and media folder."""

    def __init__(self, notes_dir: Path, media_dir: Path):
        self.notes_dir = notes_dir
        self.media_dir = media_dir
        # lowercased note stem -> (url, title)
        self.notes: dict[str, tuple[str, str]] = {}
        seen: dict[str, str] = {}
        for p in sorted(notes_dir.glob("*.md")):
            url = slugify(p.stem) + ".html"
            if url in seen:
                print(
                    f"warning: {p.name!r} and {seen[url]!r} both map to {url}; "
                    f"{p.name!r} wins",
                    file=sys.stderr,
                )
            seen[url] = p.name
            self.notes[p.stem.lower()] = (url, p.stem)
        # output media filename -> source path (only the ones actually embedded)
        self.used_media: dict[str, Path] = {}
        self.missing: list[str] = []

    def _find_media(self, target: str) -> Path | None:
        name = Path(target).name
        candidates = [
            self.media_dir / target,
            self.media_dir / name,
            self.notes_dir / target,
            self.notes_dir / name,
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None

    def render_embed(self, target: str, alt: str) -> str:
        name = Path(target).name
        if re.match(r"(https?:)?//|data:", target):
            ref, ext = target, Path(name).suffix.lower()  # external URL, pass through
        else:
            src = self._find_media(target)
            if src is None:
                self.missing.append(f"media: {target}")
                return html.escape(alt or name)  # broken embed -> plain text
            ref = media_out_name(name)
            self.used_media[ref] = src
            ext = Path(name).suffix.lower()
        alt_text = html.escape(alt or Path(name).stem, quote=True)
        ref = html.escape(ref, quote=True)
        if ext in VIDEO_EXT:
            return f'<video src="{ref}" controls></video>'
        if ext in AUDIO_EXT:
            return f'<audio src="{ref}" controls></audio>'
        return f'<img src="{ref}" alt="{alt_text}">'

    def render_wikilink(self, inner: str) -> str:
        link, _, alias = inner.partition("|")
        target, _, anchor = link.partition("#")
        display = alias.strip() or link.strip()
        entry = self.notes.get(target.strip().lower())
        if entry is None:
            self.missing.append(f"link: {link.strip()}")
            return html.escape(display)  # unknown target -> plain text
        href = entry[0]
        if anchor:
            href += "#" + slugify(anchor)
        return f'<a href="{href}">{html.escape(display)}</a>'

    def preprocess(self, body: str) -> str:
        def repl_wikilink(m: re.Match) -> str:
            bang, inner = m.group(1), m.group(2)
            if bang:
                tgt, _, alt = inner.partition("|")
                return self.render_embed(tgt.strip(), alt.strip())
            return self.render_wikilink(inner)

        body = WIKILINK_RE.sub(repl_wikilink, body)
        body = MD_IMAGE_RE.sub(
            lambda m: self.render_embed(m.group(2), m.group(1)), body
        )
        return loosen_lists(body)


def clean_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def main() -> int:
    notes_dir, media_dir, out_dir = load_config()
    if not notes_dir.is_dir():
        print(f"note folder not found: {notes_dir}", file=sys.stderr)
        return 1
    if out_dir in (notes_dir, media_dir, ROOT) or out_dir in notes_dir.parents:
        print(f"refusing to build into {out_dir} (it holds sources)", file=sys.stderr)
        return 1

    md_files = sorted(notes_dir.glob("*.md"))
    renderer = Renderer(notes_dir, media_dir)
    # No `sane_lists`: Obsidian lets a list start without a blank line above it.
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc"],
        output_format="html5",
    )
    env = Environment(loader=FileSystemLoader(str(ROOT)), autoescape=True)
    post_tmpl = env.get_template("post.jinja")
    index_tmpl = env.get_template("index.jinja")

    posts = []
    for path in md_files:
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        md.reset()
        content = md.convert(renderer.preprocess(body))
        created = parse_date(meta.get("created"))
        updated = parse_date(meta.get("updated"))
        # Thumbnail source: explicit `cover` front-matter, else the first
        # embedded image. Kept as a source path here; scaled down after the
        # output dir is cleaned (see make_thumb).
        cover = meta.get("cover")
        if cover:
            thumb_src = renderer._find_media(str(cover))
            if thumb_src is None:
                renderer.missing.append(f"cover: {cover}")
        else:
            m = re.search(r'<img\b[^>]*\bsrc="([^"]+)"', content)
            thumb_src = renderer.used_media.get(m.group(1)) if m else None

        title = path.stem.replace("﹕", ":").replace("﹖", "?")
        posts.append(
            {
                "title": title,  
                "url": slugify(path.stem) + ".html",
                "created": created.isoformat() if created else None,
                "updated": updated.isoformat() if updated else None,
                "content": content,
                "thumb": None,  # set by the thumbnail pass below
                "_thumb_src": thumb_src,
                "_created": created,
            }
        )

    # Reverse-chronological by `created`; undated posts sink to the bottom.
    dated = sorted(
        (p for p in posts if p["_created"]), key=lambda p: p["_created"], reverse=True
    )
    undated = sorted(
        (p for p in posts if not p["_created"]), key=lambda p: p["title"].lower()
    )
    ordered = dated + undated

    clean_dir(out_dir)

    # Scale each post's thumbnail source down to a small square WebP; if it
    # can't be rasterised, link the original file instead (copied below).
    for post in posts:
        src = post.pop("_thumb_src", None)
        if src is None:
            continue
        name = make_thumb(src, out_dir)
        if name is None:
            name = media_out_name(src.name)
            renderer.used_media.setdefault(name, src)
        post["thumb"] = name

    for post in posts:
        (out_dir / post["url"]).write_text(post_tmpl.render(**post), encoding="utf-8")
    (out_dir / "index.html").write_text(
        index_tmpl.render(posts=ordered), encoding="utf-8"
    )

    for out_name, src in renderer.used_media.items():
        shutil.copy2(src, out_dir / out_name)

    for name in ("styles.css", "favicon.ico"):
        asset = ROOT / name
        if asset.exists():
            shutil.copy2(asset, out_dir / name)

    shown = os.path.relpath(out_dir)
    print(
        f"built {len(posts)} post(s), "
        f"{len(renderer.used_media)} media file(s) -> {shown}/"
    )
    for item in dict.fromkeys(renderer.missing):
        print(f"  unresolved {item}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
