"""One-time port of the Weebly export to hugo-book content.

    uv run --with selectolax --with markdownify --with pillow python -I tools/import.py <export_dir>

Writes content/, static/uploads/ (originals, plus .webp copies of big images), and
tools/import-report.txt. Re-running regenerates content/ from scratch.
"""
import html
import re
import shutil
import sys
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from markdownify import MarkdownConverter
from PIL import Image
from selectolax.lexbor import LexborHTMLParser as P

SRC = Path(sys.argv[1])
ROOT = Path(__file__).resolve().parent.parent
CONTENT, STATIC = ROOT / 'content', ROOT / 'static'
SITE_NAME = 'أرشيف المترجمين'
DROP = ('style, script, ins, noscript, form, iframe[src*="commentsplugin"], .wsite-adsense, .wsite-spacer, '
        '.imgPusher, .blog-comments, .blog-comments-bottom, .blog-sidebar, .blog-page-nav-previous, '
        '.blog-social, .blog-feed-link, .wsite-form-container, #wsite-search-sidebar, '
        'a[href*="youtubeembedcode.com"], a[href*="add-link-exchange.com"], '  # SEO spam from an embed generator
        '#fanback, iframe[src*="widgetic.com"]')  # Facebook like-box pop-up; widgetic widgets now serve blank pages
SEARCH = '''<link href="/pagefind/pagefind-ui.css" rel="stylesheet">
<div id="search"></div>
<script src="/pagefind/pagefind-ui.js"></script>
<script>new PagefindUI({ element: '#search', showSubResults: false, autofocus: true })</script>'''
report: list[str] = []
blocks: list[str] = []
used_images: set[str] = set()


def stash(node, md: str):
    blocks.append(md)
    node.replace_with(P(f'<p>@@B{len(blocks) - 1}@@</p>').css_first('p'))


class MD(MarkdownConverter):
    # Weebly nests/empties bold tags and glues them to Arabic letters, which breaks `**` flanking
    # rules (193 literal ** on 28 pages); inline HTML tags always render and still allow markdown inside
    def convert_b(self, el, text, parent_tags):
        return f'<strong>{text}</strong>' if text.strip() and not {'b', 'strong'} & set(parent_tags) else text

    def convert_i(self, el, text, parent_tags):
        return f'<em>{text}</em>' if text.strip() and not {'i', 'em'} & set(parent_tags) else text

    convert_strong, convert_em = convert_b, convert_i


def md_of(node) -> str:
    # inner html in a div so <td> content isn't converted as an inline table cell
    out = MD(heading_style='ATX', strip=['font', 'span', 'u']).convert(f'<div>{node.inner_html or ""}</div>')
    out = re.sub(r'[ \t]+\n', '\n', out.replace('\xa0', ' '))
    out = re.sub('[\ufeff\u200b]', '', out)
    return re.sub(r'\n{3,}', '\n\n', out).strip()


def to_md(node) -> str:
    for n in reversed(node.css(DROP)):
        n.decompose()
    for n in reversed(node.css('.wsite-youtube')):
        f = n.css_first('iframe')
        stash(n, youtube(f.attributes.get('src') or '') if f else '')
    for n in reversed(node.css('iframe')):  # raw embeds pasted into Weebly "custom HTML" blocks
        src = html.unescape(n.attributes.get('src') or '').strip()
        if 'youtube' in src:
            stash(n, youtube(src))
        elif re.match(r'^(https?:)?//[^/]', src) and not re.search(r'facebook\.com|//(/|$)|weebly', src):
            # framing other sites mostly fails (X-Frame-Options, http on https), so link instead
            host = re.sub(r'^(https?:)?//(www\.)?', '', src).split('/')[0]
            stash(n, f'[{host}]({re.sub(r"^(http:)?//", "https://", src) if host.endswith("google.com") else src})')
        else:
            n.decompose()
    for n in reversed(node.css('audio')):
        s = n.attributes.get('src') or (n.css_first('source') and n.css_first('source').attributes.get('src')) or ''
        stash(n, f'<audio controls preload="none" src="{html.escape(re.sub(r"^(http:)?//", "https://", s))}"></audio>' if s else '')
    for n in reversed(node.css('.wsite-flash')):
        e, movie = n.css_first('embed'), n.css_first('param[name="movie"]')
        src = (e and e.attributes.get('src')) or (movie and movie.attributes.get('value'))
        stash(n, f'{{{{< flash "/{clean(src)}" >}}}}' if src else '')
    for n in reversed(node.css('a.wsite-button')):
        href = link(n.attributes.get('href') or '')
        label = ' '.join(n.text(separator=' ').split())
        dead = not href or href.startswith('javascript:')  # placeholder buttons that never linked anywhere
        stash(n, '' if dead else f'{{{{< button href="{href}" >}}}}{label}{{{{< /button >}}}}')
    for n in reversed(node.css('.accordion__item')):
        title = ' '.join(n.css_first('.accordion__title').text(separator=' ').split()).replace('"', "'")
        body = n.css_first('.accordion__content')
        for img in body.css('img'):
            if (img.attributes.get('alt') or 'Picture') == 'Picture':
                img.attrs['alt'] = title
        inner = to_md(body)
        stash(n, f'{{{{% details title="{title}" %}}}}\n{inner}\n{{{{% /details %}}}}' if inner else '')  # empty template items
    for n in reversed(node.css('.wsite-multicol')):
        cols = [c for c in (to_md(td) for td in n.css('td.wsite-multicol-col')) if c.strip()]
        items = '\n'.join('- ' + c.replace('\n', '\n  ') for c in cols)
        # hugo-book columns can't nest (the inner one renders empty and truncates the page): stack inner ones
        nested = any('wsite-multicol' in (a.attributes.get('class') or '').split() for a in ancestors(n))
        flat = len(cols) < 2 or nested
        stash(n, '\n\n'.join(cols) if flat else f'{{{{% columns %}}}}\n{items}\n{{{{% /columns %}}}}')
    for img in node.css('img'):
        img.attrs['src'] = image(img.attributes.get('src') or '')
        if (img.attributes.get('alt') or '') == 'Picture':
            img.attrs['alt'] = ''
    for a in reversed(node.css('a')):
        href = a.attributes.get('href') or ''
        if re.match(r'^(https?:)?//(unset)?(/|$)', href, re.I):
            a.unwrap()  # Weebly blog permalinks on an "UNSET" host; the posts weren't exported
        elif href:
            a.attrs['href'] = link(href)
    md = md_of(node)
    return re.sub(r'@@B(\d+)@@', lambda m: blocks[int(m.group(1))], md)


def ancestors(n):
    while (n := n.parent) is not None:
        yield n


def youtube(src: str) -> str:
    if m := re.search(r'[?&]list=([\w-]+)', src):
        return f'{{{{< youtube list="{m.group(1)}" >}}}}'
    m = re.search(r'(?:embed/|v=|youtu\.be/)([\w-]{11})', src)
    # "videoseries" is also 11 chars: a playlist embed with an empty list= is broken in the original too
    return f'{{{{< youtube {m.group(1)} >}}}}' if m and m.group(1) != 'videoseries' else ''


def clean(src: str) -> str:
    return unquote(re.sub(r'^(https?:)?//translearner\.weebly\.com/|^/+', '', src.split('?')[0]))


def image(src: str) -> str:
    if re.match(r'^(https?:)?//', src) and 'translearner.weebly.com' not in src:
        return re.sub(r'^(http:)?//', 'https://', src)
    path = clean(src)
    used_images.add(path)
    return '/' + path


def link(href: str) -> str:
    href = html.unescape(href)
    if 'l.facebook.com/l.php' in href:  # unwrap Facebook's tracking redirect
        href = parse_qs(urlparse(href).query).get('u', [href])[0]
    if re.match(r'^(https?:)?//translearner\.weebly\.com', href) or not re.match(r'^([a-z]+:|//|#|/)', href):
        local = clean(href)
        if local.endswith('.html') or local.startswith(('uploads/', 'files/')):
            return '/' + local
    return href


SPLIT_LINK = re.compile(r'(<a href="([^"]+)"[^>]*>)((?:(?!</a>).)*)</a>(\s*)<a href="\2"[^>]*>', re.S)


def merge_links(src: str) -> str:
    # Weebly splits one link into adjacent <a>s with the same href ("قاموس&nbsp;" + "مكتبة لبنان");
    # merging keeps the space that markdownify would trim from the first link's text
    while (n := SPLIT_LINK.sub(r'\1\3\4', src)) != src:
        src = n
    return src


def page(fname: str):
    d = P(merge_links((SRC / fname).read_text(encoding='utf8')))
    title = html.unescape(d.css_first('title').text()).strip()
    title = re.sub(rf'\s*-\s*({SITE_NAME}|Translators Archive)\s*$', '', title)
    title = re.sub(rf'\s*-\s*({SITE_NAME}|Translators Archive)\s*$', '', title)
    # the header banner holds the page heading and, on game pages, the Flash embed
    banner = d.css_first('#banner .wsite-section-elements')
    c = d.css_first('#wsite-content')
    top, body = (to_md(banner) if banner else ''), (to_md(c) if c else '')
    if not body and '{{< flash' not in top:
        return title, ''  # a banner heading alone is still an empty page
    return title, '\n\n'.join(p for p in (top, body) if p)


def write(path: Path, front: dict, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in front.items():
        if isinstance(v, bool):
            lines.append(f'{k}: {str(v).lower()}')
        elif isinstance(v, int):
            lines.append(f'{k}: {v}')
        elif isinstance(v, list):
            lines.append(f'{k}:' + ''.join(f'\n  - "{x}"' for x in v))
        else:
            lines.append(f'{k}: "' + v.replace('\\', '\\\\').replace('"', '\\"') + '"')
    path.write_text('---\n' + '\n'.join(lines) + f'\n---\n\n{body}\n', encoding='utf8')


sections: dict[str, Path] = {}  # menu label -> section _index.md


def walk(ul, parent: Path, seen: set):
    weight = 0
    for li in (c for c in ul.iter() if c.tag == 'li'):
        a = li.css_first('a')
        label = a.css_first('.wsite-menu-title') or a
        label = label.text(separator=' ', strip=True)
        href = (a.attributes.get('href') or '').split('?')[0]
        sub_ul = li.css_first('ul')
        if href == 'index.html':
            continue
        weight += 1
        # non-clickable menu parents have no page; Weebly's li id is a stable slug
        stem = href[:-5] if href.endswith('.html') else (li.attributes.get('id') or f'section-{weight}').removeprefix('pg').removeprefix('wsite-nav-')
        title, body = page(href) if href.endswith('.html') and (SRC / href).exists() else (label, '')
        seen.add(href)
        front = {'title': title or label, 'linkTitle': label, 'weight': weight}
        if href.endswith('.html'):
            front['url'] = '/' + href
        if sub_ul:
            front['bookCollapseSection'] = True
            write(parent / stem / '_index.md', front, body)
            sections.setdefault(label, parent / stem / '_index.md')
            walk(sub_ul, parent / stem, seen)
        else:
            write(parent / f'{stem}.md', front, body)


def fetch_missing():
    # an original that already has its .webp copy was pruned on purpose; don't fetch it back
    missing = (p for p in used_images if p.startswith('uploads/') and not (STATIC / p).exists()
               and not (STATIC / p).with_suffix('.webp').exists())
    for path in sorted(missing):
        url = f'https://web.archive.org/web/2026id_/https://translearner.weebly.com/{quote(path)}'
        (STATIC / path).parent.mkdir(parents=True, exist_ok=True)
        # curl -m caps the whole transfer (urllib's per-read timeout hung on archive.org);
        # archive.org refuses connections (curl exit 7) when rate limited, so back off and retry
        for wait in (0, 30, 120, 300):
            time.sleep(wait)
            r = subprocess.run(['curl', '-sfL', '-m', '90', url], capture_output=True)
            if r.returncode != 7:
                break
        if r.returncode == 0 and r.stdout:
            (STATIC / path).write_bytes(r.stdout)
            report.append(f'recovered from wayback: {path}')
        else:
            report.append(f'MISSING image (curl exit {r.returncode}): {path}')
        time.sleep(2)


def webp_copies(max_w=1200, min_bytes=150_000):
    """Big jpg/png images get a lighter .webp sibling that pages point at (originals are pruned later)."""
    swapped = {}
    for path in used_images:
        f = STATIC / path
        out = f.with_suffix('.webp')
        if f.suffix.lower() in ('.jpg', '.jpeg', '.png') and out.exists() and not f.exists():
            swapped['/' + path] = '/' + str(out.relative_to(STATIC))  # original already pruned
            continue
        if f.suffix.lower() not in ('.jpg', '.jpeg', '.png') or not f.exists() or f.stat().st_size < min_bytes:
            continue
        if not out.exists():
            with Image.open(f) as im:
                im.thumbnail((max_w, max_w * 10))
                im.save(out, 'WEBP', quality=80)
        if out.stat().st_size < f.stat().st_size:
            swapped['/' + path] = '/' + str(out.relative_to(STATIC))
        else:
            out.unlink()
    for md in CONTENT.rglob('*.md'):
        t = md.read_text(encoding='utf8')
        n = re.sub(r'\]\((/uploads/[^)\s]+)\)', lambda m: f']({swapped.get(m.group(1), m.group(1))})', t)
        if n != t:
            md.write_text(n, encoding='utf8')
    report.append(f'webp copies used: {len(swapped)}')


# kept although no page links them: header photos (no slot in the theme yet), the two apps, founder sketch
KEEP = re.compile(r'/background-images/|\.apk$|ammqe-sketch_orig\.jpg$')


def prune_unused():
    """Drop uploads no page references (Weebly media-library leftovers, originals behind .webp copies).
    They all remain in the original export zip."""
    texts = [f.read_text(encoding='utf8') for f in [*CONTENT.rglob('*.md'), *(ROOT / 'layouts').rglob('*.html')]]
    used = {unquote(m) for t in texts for m in re.findall(r'/(uploads/[^)"\'\s]+)', t)}
    gone = 0
    for f in sorted((STATIC / 'uploads').rglob('*')):
        rel = str(f.relative_to(STATIC))
        if f.is_file() and rel not in used and not KEEP.search(rel):
            gone += f.stat().st_size
            f.unlink()
    for d in sorted((STATIC / 'uploads').rglob('*'), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    report.append(f'pruned unreferenced uploads: {gone / 1048576:.0f} MB')


def main():
    shutil.rmtree(CONTENT, ignore_errors=True)
    if not (STATIC / 'uploads').exists():
        shutil.copytree(SRC / 'uploads', STATIC / 'uploads')
    home = P((SRC / 'index.html').read_text(encoding='utf8'))
    seen = {'index.html'}
    walk(home.css_first('ul.wsite-menu-default'), CONTENT, seen)
    empty = []
    for o in sorted(p.name for p in SRC.glob('*.html') if p.name not in seen):
        t, b = page(o)
        if not b:
            # empty Weebly pages were the section headers' pages: redirect to that section, else home
            if t in sections:
                f = sections[t]
                txt = f.read_text(encoding='utf8')
                if 'aliases:\n' not in txt:
                    txt = txt.replace('---\n', '---\naliases:\n', 1)
                f.write_text(txt.replace('aliases:\n', f'aliases:\n  - "/{o}"\n', 1), encoding='utf8')
            else:
                empty.append('/' + o)
            continue
        write(CONTENT / 'pages' / f'{o[:-5]}.md', {'title': t, 'url': '/' + o, 'bookHidden': True}, b)
    write(CONTENT / 'pages' / '_index.md', {'title': 'صفحات', 'bookHidden': True}, '')
    title, body = page('index.html')
    write(CONTENT / '_index.md', {'title': title, 'aliases': empty}, body)
    write(CONTENT / 'search.md', {'title': 'بحث', 'url': '/search_results.html', 'bookHidden': True}, SEARCH)
    fetch_missing()
    webp_copies()
    prune_unused()
    leftovers = sorted({m for md in CONTENT.rglob('*.md') for m in re.findall(r'<(\w+)[ >]', md.read_text(encoding='utf8'))})
    report.append(f'menu pages: {len(seen)}, hidden pages: {len(list((CONTENT / "pages").glob("*.md"))) - 1}, '
                  f'empty pages redirected: {len(empty)}')
    report.append(f'raw html tags left in markdown: {leftovers}')
    (ROOT / 'tools' / 'import-report.txt').write_text('\n'.join(report) + '\n', encoding='utf8')
    print('\n'.join(report[-3:]))


main()
