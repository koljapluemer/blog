# blog

A tiny static site generator (`ssg.py`) that turns a flat folder of Obsidian
Markdown notes into a plain HTML site.

## Usage

```sh
uv run ssg.py
```

Output is written to `_site/` (wiped and rebuilt each run). Serve it with any
static host, or locally:

```sh
python -m http.server -d _site
```

## Configuration — `config.json`

| key                 | meaning                                  | default  |
| ------------------- | ---------------------------------------- | -------- |
| `obsidianPath`      | folder containing the `.md` notes        | `.`      |
| `obsidianMediaPath` | folder containing embedded media         | `.`      |
| `outputPath`        | build target                             | `_site`  |

Paths are resolved relative to the project root. The notes folder is assumed
flat (no subfolders); every `*.md` file in it becomes a post.

## What it does

- **One page per note.** The file name (minus `.md`) is the post title; the URL
  is a slugified version of it.
- **Index page**, posts sorted newest-first by the `created` front-matter date.
  Notes without `created` sort to the bottom and show no date.
- **Dates.** `created` and `updated` (from YAML front-matter) are shown on the
  post and in the index list when present.
- **`[[Note]]` / `[[Note|alias]]`** become links to that post (`[[Note#heading]]`
  keeps the anchor). A link to a note that doesn't exist renders as plain text.
- **`![[file.png]]` / `![[file.png|alt]]`** and standard `![alt](file.png)`
  become `<img>` / `<video>` / `<audio>` depending on extension. `alt` is the
  bracket text. Only media actually embedded somewhere is copied into `_site/`;
  `http(s)`/`data:` URLs are passed through untouched.

## Templates

`index.jinja` (gets `posts`) and `post.jinja` (gets `title`, `created`,
`updated`, `content`) are plain Jinja2. `styles.css` is copied to `_site/` as-is.
