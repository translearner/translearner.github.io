# أرشيف المترجمين — Translators Archive

**https://translearner.github.io**

A static port of the old `translearner.weebly.com` site, built with [Hugo](https://gohugo.io) and the
[hugo-book](https://github.com/alex-shpak/hugo-book) theme (RTL). Deployed to GitHub Pages on every push to `main`.

## Develop

```sh
git clone --recurse-submodules https://github.com/translearner/translearner.github.io
mise install        # Hugo + uv, versions pinned in mise.toml
mise run dev        # http://localhost:1313
mise run build      # public/ + Pagefind search index
```

## Content

- `content/` mirrors the old site menu: a folder per menu section (`_index.md`), a `.md` per page.
  Pages that weren't in the menu live in `content/pages/` (hidden from the sidebar).
- Every page keeps its old Weebly address through `url:` in its front matter, so old links keep working.
- Files and images are in `static/uploads/` at their original Weebly paths. Large images were converted
  to `.webp`; uploads no page links to were left out (they are still in the original Weebly export).
- The font is IBM Plex Sans Arabic, self-hosted in `static/fonts/` (SIL OFL).
- Shortcodes: `youtube` (loads the player on click), `flash` (old Flash games via Ruffle), and hugo-book's
  `details`, `columns` and `button`.

## Deploy

Pushing to `main` runs `.github/workflows/pages.yml`: Hugo build, Pagefind index, internal link check,
then deploy. Pages must be set to "GitHub Actions" in the repo settings.

## Re-importing from the Weebly export

`tools/import.py` converted the Weebly export once. It overwrites `content/`, so only re-run it if you
want to start over:

```sh
uv run --with selectolax --with markdownify --with pillow python -I tools/import.py <unzipped-export-dir>
```

It also re-downloads missing images from the Wayback Machine and writes `tools/import-report.txt`.
