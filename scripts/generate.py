# scripts/generate.py
# Forbes-like Light Theme + Topic extraction + Spring Walk link enforcement

import os
import re
import html
import json
import feedparser
from datetime import datetime, timedelta
from dateutil import tz
from pathlib import Path
from typing import List, Dict, Any

from openai import OpenAI

# ===== Config =====
FEEDS = [
    "https://www.epravo.cz/rss.php",
    "https://advokatnidenik.cz/feed/",
    "https://www.pravniprostor.cz/rss/aktuality",
]

OPENAI_MODEL = os.getenv("MODEL_NAME", "gpt-4o-mini")
DAYS_BACK = int(os.getenv("DAYS_BACK", "30"))

INPUT_PRICE_PER_MTOK = float(os.getenv("INPUT_PRICE_USD_PER_MTOK", "0.15"))
OUTPUT_PRICE_PER_MTOK = float(os.getenv("OUTPUT_PRICE_USD_PER_MTOK", "0.60"))
USD_TO_CZK = float(os.getenv("USD_TO_CZK", "24.5"))

OUTPUT_DIR = Path(".")
POSTS_DIR = OUTPUT_DIR / "posts"
POSTS_DIR.mkdir(exist_ok=True)

API_KEY = os.environ.get("OPENAI_API_KEY")
if not API_KEY:
    raise RuntimeError("Chybí OPENAI_API_KEY v env.")

client = OpenAI(api_key=API_KEY)

TOK_IN = 0
TOK_OUT = 0

# ===== Utility =====
def add_usage(resp):
    global TOK_IN, TOK_OUT
    try:
        u = resp.usage
        TOK_IN += int(getattr(u, "prompt_tokens", getattr(u, "input_tokens", 0)) or 0)
        TOK_OUT += int(getattr(u, "completion_tokens", getattr(u, "output_tokens", 0)) or 0)
    except Exception:
        pass

def to_cz_date(dt: datetime) -> str:
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz.UTC)
    return dt.astimezone(tz.gettz("Europe/Prague")).strftime("%-d. %-m. %Y %H:%M") if dt else "neznámo"

def to_cz_day(dt: datetime) -> str:
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz.UTC)
    return dt.astimezone(tz.gettz("Europe/Prague")).strftime("%-d. %-m. %Y")

def parse_pubdate(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    return datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=tz.UTC)

def slugify(text: str) -> str:
    text = text.lower()
    replace_map = {
        ord('á'): 'a', ord('č'): 'c', ord('ď'): 'd', ord('é'): 'e', ord('ě'): 'e',
        ord('í'): 'i', ord('ň'): 'n', ord('ó'): 'o', ord('ř'): 'r', ord('š'): 's',
        ord('ť'): 't', ord('ú'): 'u', ord('ů'): 'u', ord('ý'): 'y', ord('ž'): 'z'
    }
    text = text.translate(replace_map)
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text or "clanek"

def escape_html(s: str) -> str:
    return html.escape(s, quote=True)

def md_links_to_html(text: str) -> str:
    return re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)',
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)

def md_to_html(txt: str) -> str:
    """
    Odstavce = oddělené prázdnou řádkou.
    Zachová h2/h3, převádí markdown odkazy na <a>.
    """
    txt = md_links_to_html(txt)
    parts = [p.strip() for p in re.split(r"\n\s*\n", txt.strip()) if p.strip()]
    html_pars = []
    for p in parts:
        if p.startswith("## "):
            html_pars.append(f"<h2>{escape_html(p[3:].strip())}</h2>")
        elif p.startswith("### "):
            html_pars.append(f"<h3>{escape_html(p[4:].strip())}</h3>")
        else:
            html_pars.append(f"<p>{p}</p>")
    return "\n".join(html_pars)


# ===== LLM =====
def llm_classify_relevance(title: str, summary: str) -> int:
    prompt = f"""
Ohodnoť poutavost článku (1–5) pro odborný právnický blog:
Titulek: {title}
Anotace: {summary or "(bez anotace)"}
Vrať pouze číslo.
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0
    )
    add_usage(resp)
    m = re.search(r"[1-5]", resp.choices[0].message.content or "")
    return int(m.group()) if m else 2

def ensure_springwalk_link(article_md: str) -> str:
    if "springwalk.cz" not in article_md.lower():
        article_md += "\n\nDalší informace nabízí [právní poradenství Spring Walk](https://www.springwalk.cz/pravni-poradenstvi/)."
    return article_md

def llm_generate_article(title: str, summary: str, source_url: str) -> str:
    user = f"""
Napiš česky článek pro širokou veřejnost (3–5 odstavců), srozumitelný, bez žargonu.
- Použij 1–2 mezititulky
- Uveď 1× odkaz na původní zdroj: [zdroj]({source_url})
- Přirozeně vlož odkaz na [právní poradenství Spring Walk](https://www.springwalk.cz/pravni-poradenstvi/)
- Drž se faktů z podkladu
Podklad:
Titulek: {title}
Anotace: {summary}
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": user}],
        temperature=0.5,
        max_tokens=1200
    )
    add_usage(resp)
    md = resp.choices[0].message.content or ""
    return ensure_springwalk_link(md.strip())

def llm_generate_linkedin_posts(title: str, summary: str) -> List[str]:
    """
    Vrátí 3 HTML bloky s jasným oddělením + tučný nadpis sekce.
    Sekce: Společnost Spring Walk / Jednatel (formální) / Jednatel (hravý)
    """
    user = f"""
Vytvoř 3 delší příspěvky na LinkedIn (4–6 vět) k tématu níže.
Každý blok začni přesným nadpisem (bez mřížek, bez Markdownu):
"Společnost Spring Walk:"
"Jednatel (formální):"
"Jednatel (hravý):"
Poté napiš text. Nepoužívej odrážky. Bloky odděl třemi pomlčkami ---.
Podklad:
Titulek: {title}
Anotace: {summary or "(bez anotace)"}
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": user}],
        temperature=0.7,
        max_tokens=750,
    )
    add_usage(resp)
    raw = (resp.choices[0].message.content or "").strip()

    # Rozdělit podle --- a vytáhnout nadpis + tělo. Odstraníme případné "### ".
    blocks = [b.strip() for b in re.split(r'\n?---\n?', raw) if b.strip()]
    result = []
    wanted = [
        r"Společnost Spring Walk:\s*",
        r"Jednatel\s*\(formální\):\s*",
        r"Jednatel\s*\(hravý\):\s*",
    ]
    for i in range(min(3, len(blocks))):
        b = re.sub(r"^\s*#+\s*", "", blocks[i])  # odstranit případné ### na začátku
        m = re.match(wanted[i], b, flags=re.IGNORECASE)
        if m:
            heading = m.group(0).rstrip(": ").rstrip()
            body = b[m.end():].strip()
        else:
            # fallback – první věta jako heading, zbytek jako body
            lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
            heading = lines[0].rstrip(":") if lines else "Příspěvek"
            body = " ".join(lines[1:]) if len(lines) > 1 else ""

        # HTML blok (vlastní wrapper pro stylování a rozestupy)
        block_html = f'''
<div class="li-post">
  <div class="li-heading"><strong>{escape_html(heading)}</strong></div>
  <div class="li-body">{escape_html(body)}</div>
</div>'''.strip()
        result.append(block_html)

    # doplň prázdné, kdyby LLM nevrátilo dost
    while len(result) < 3:
        result.append('''<div class="li-post"><div class="li-heading"><strong>Příspěvek</strong></div><div class="li-body"></div></div>''')
    return result[:3]


# ===== CSS Light Theme =====
BASE_CSS = """
:root {
  --bg: #ffffff;
  --panel: #ffffff;
  --text: #111111;
  --muted: #555555;
  --accent: #0056b3;
}
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, Arial, Helvetica, sans-serif;
  line-height: 1.7;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 32px 20px 56px; }
h1, h2 {
  font-family: Inter, Arial, Helvetica, sans-serif;
  font-weight: 700;
}
.meta {
  color: var(--muted);
  font-family: Georgia, 'Times New Roman', serif;
  font-style: italic;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 20px;
}
.card {
  background: var(--panel);
  padding: 20px;
  border: 1px solid #ddd;
  box-shadow: 0 2px 8px rgba(0,0,0,.05);
  transition: transform .2s ease, box-shadow .2s ease;
}
.card:hover {
  transform: translateY(-4px);
  box-shadow: 0 4px 12px rgba(0,0,0,.1);
}
.badge {
  display: inline-block;
  padding: 4px 8px;
  background: #f0f0f0;
  border-radius: 4px;
  font-size: 0.8em;
  margin-bottom: 6px;
}
.article {
  font-family: Georgia, 'Times New Roman', serif;
  background: var(--panel);
  padding: 20px;
  border: 1px solid #ddd;
  box-shadow: 0 2px 8px rgba(0,0,0,.05);
}
  /* LinkedIn blocks */
  .li-post { margin: 14px 0 18px; padding-bottom: 12px; border-bottom: 1px solid #eee; }
  .li-heading { margin-bottom: 6px; }
  .li-body { line-height: 1.7; }

"""

# ===== HTML =====
def render_post_html(title, article_html, posts, rating, source_url, pub_date_str, topic, cost_line=""):
    esc_title = escape_html(title)
    esc_url = escape_html(source_url or "#")
    topic_html = f'<div class="badge">{escape_html(topic)}</div>' if topic else ""
    posts_html = "".join(f"<li>{p}</li>" for p in posts)
    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<title>{esc_title}</title>
<style>{BASE_CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <a href="../index.html">← Přehled</a>
  {topic_html}
  <h1>{esc_title}</h1>
  <div class="meta">Poutavost: {rating}/5 · Zdroj: <a href="{esc_url}" target="_blank">odkaz</a> · Publikováno: {escape_html(pub_date_str)}</div>
</header>
<article class="article">
{article_html}
<hr>
<h2>Tipy na LinkedIn</h2>
<ul>{posts_html}</ul>
</article>
<div class="meta">{escape_html(cost_line)}</div>
</div>
</body>
</html>
"""

def render_index_html(items, cost_line="", range_line="", counts_line=""):
    cards = []
    for it in items:
        topic_html = f'<div class="badge">{escape_html(it["topic"])}</div>' if it.get("topic") else ""
        cards.append(f"""<a class="card" href="{escape_html(it['href'])}">
  {topic_html}
  <h2>{escape_html(it['title'])}</h2>
  <div class="meta">{escape_html(it['source'])} · {escape_html(it['pub_date_str'])}</div>
  <div class="meta">Poutavost: {it['rating']}/5</div>
</a>""")
    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<title>Právní novinky</title>
<style>{BASE_CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Právní novinky – AI generované</h1>
  <div class="meta">{escape_html(range_line)}</div>
  <div class="meta">{escape_html(counts_line)}</div>
</header>
<div class="grid">
{''.join(cards)}
</div>
<div class="meta">{escape_html(cost_line)}</div>
</div>
</body>
</html>
"""

# ===== Main =====
def main():
    now_utc = datetime.now(tz.UTC)
    cutoff = now_utc - timedelta(days=DAYS_BACK)
    seen_links = set()
    collected = []
    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        source_name = getattr(feed.feed, "title", feed_url)
        for e in feed.entries:
            link = getattr(e, "link", "")
            if not link or link in seen_links:
                continue
            pub_dt = parse_pubdate(e)
            if pub_dt and pub_dt < cutoff:
                continue
            title = (getattr(e, "title", "") or "").strip()
            summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            categories = getattr(e, "tags", [])
            topic = categories[0]["term"] if categories else ""
            seen_links.add(link)
            collected.append({
                "title": title, "summary": summary, "link": link,
                "source": source_name, "pub_dt": pub_dt, "topic": topic
            })

    selected = []
    for item in collected:
        rating = llm_classify_relevance(item["title"], item["summary"])
        item["rating"] = rating
        if rating >= 3:
            selected.append(item)

    render_queue = []
    index_items = []
    for item in selected:
        article_md = llm_generate_article(item["title"], item["summary"], item["link"])
        article_html = md_to_html(article_md)
        posts = llm_generate_linkedin_posts(item["title"], item["summary"])
        slug = slugify(item["title"])[:60]
        fn = POSTS_DIR / f"{slug}.html"
        render_queue.append({
            "filepath": fn, "title": item["title"], "article_html": article_html,
            "posts": posts, "rating": item["rating"], "link": item["link"],
            "pub_date_str": to_cz_date(item["pub_dt"]), "topic": item["topic"]
        })
        index_items.append({
            "title": item["title"], "href": f"posts/{fn.name}", "rating": item["rating"],
            "source": item["source"], "pub_date": item["pub_dt"] or datetime.min.replace(tzinfo=tz.UTC),
            "pub_date_str": to_cz_date(item["pub_dt"]), "topic": item["topic"]
        })

    index_items.sort(key=lambda x: x["pub_date"], reverse=True)

    cost_usd = (TOK_IN * (INPUT_PRICE_PER_MTOK/1_000_000)) + (TOK_OUT * (OUTPUT_PRICE_PER_MTOK/1_000_000))
    cost_czk = cost_usd * USD_TO_CZK
    cost_line = f"Odhad nákladů: ${cost_usd:.4f} (~{cost_czk:.2f} Kč) · Input {TOK_IN} · Output {TOK_OUT} · Model {OPENAI_MODEL}"
    range_line = f"Rozsah: {to_cz_day(cutoff)} – {to_cz_day(now_utc)}"
    counts_line = f"Načteno: {len(collected)} · Vybráno: {len(selected)}"

    for it in render_queue:
        html_out = render_post_html(it["title"], it["article_html"], it["posts"], it["rating"],
                                    it["link"], it["pub_date_str"], it["topic"], cost_line)
        it["filepath"].write_text(html_out, encoding="utf-8")

    index_html = render_index_html(index_items, cost_line, range_line, counts_line)
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

if __name__ == "__main__":
    main()
