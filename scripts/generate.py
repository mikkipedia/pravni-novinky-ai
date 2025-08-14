# scripts/generate.py
# -----------------------------------------
# RSS -> LLM -> statický web (light / Forbes-like)
# - sbírá české právní novinky z RSS (posledních N dní)
# - ohodnotí poutavost 1–5 (LLM)
# - pro 3–5 vygeneruje článek + 3 LinkedIn posty
# - článek VŽDY rozdělí do 2–3 sekcí s H2/H3 nadpisy
# - používá externí CSS: assets/style.css
# - délky: článek max_tokens=1280, LI posty max_tokens=650
# -----------------------------------------

import os
import re
import feedparser
from datetime import datetime, timedelta
from urllib.parse import urlparse
from html import escape as escape_html
from openai import OpenAI

# ===== Konfigurace =====
OPENAI_MODEL = os.getenv("MODEL_NAME", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Chybí OPENAI_API_KEY v env proměnných.")

client = OpenAI(api_key=OPENAI_API_KEY)

# Počet dní zpět (pro rychlý test můžeš přepsat na 1)
DAYS_BACK = int(os.getenv("DAYS_BACK", "30"))

RSS_FEEDS = [
    "https://www.epravo.cz/rss.php",
    "https://advokatnidenik.cz/feed/",
    "https://www.pravniprostor.cz/rss/aktuality",
]

# ===== Usage tracking (orientační) =====
total_prompt_tokens = 0
total_completion_tokens = 0

def add_usage(resp):
    global total_prompt_tokens, total_completion_tokens
    try:
        u = resp.usage
        pt = getattr(u, "prompt_tokens", getattr(u, "input_tokens", 0)) or 0
        ct = getattr(u, "completion_tokens", getattr(u, "output_tokens", 0)) or 0
        total_prompt_tokens += int(pt)
        total_completion_tokens += int(ct)
    except Exception:
        pass

# ===== Utility =====
def parse_pub_date(entry) -> datetime | None:
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    try:
        return datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    except Exception:
        return None

def slugify(text: str) -> str:
    text = text.lower()
    cz = {
        ord('á'): 'a', ord('č'): 'c', ord('ď'): 'd', ord('é'): 'e', ord('ě'): 'e',
        ord('í'): 'i', ord('ň'): 'n', ord('ó'): 'o', ord('ř'): 'r', ord('š'): 's',
        ord('ť'): 't', ord('ú'): 'u', ord('ů'): 'u', ord('ý'): 'y', ord('ž'): 'z',
        ord('ä'): 'a', ord('ö'): 'o', ord('ü'): 'u'
    }
    text = text.translate(cz)
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text or "clanek"

def md_links_to_html(text: str) -> str:
    # [text](url) -> <a href="url" ...>text</a>
    return re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)',
                  r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)

def md_to_html(txt: str) -> str:
    # Odstavce oddělené prázdnou řádkou, podpora ## / ### jako H2/H3
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

def ensure_springwalk_link(md: str) -> str:
    if "springwalk.cz" not in md.lower():
        md += "\n\nDalší informace nabízí [právní poradenství Spring Walk](https://www.springwalk.cz/pravni-poradenstvi/)."
    return md

def ensure_section_headings(md: str) -> str:
    """
    Pokud článek obsahuje < 2 nadpisy (##/###), rozděl text na 2–3 tematické sekce
    a vlož generické H2 nadpisy. Odkazy zůstanou zachované.
    """
    # Má už text aspoň 2 mezititulky?
    if len(re.findall(r'^\s*##\s+|^\s*###\s+', md, flags=re.MULTILINE)) >= 2:
        return md

    paras = [p.strip() for p in re.split(r'\n\s*\n', md.strip()) if p.strip()]
    if not paras:
        return md

    if len(paras) <= 3:
        # Krátký text → 2 sekce
        split = max(1, len(paras) // 2)
        parts = [paras[:split], paras[split:]]
        titles = ["## Co se stalo", "## Co z toho plyne"]
    else:
        # Delší text → 3 sekce
        third = max(1, len(paras) // 3)
        parts = [paras[:third], paras[third:2*third], paras[2*third:]]
        titles = ["## Kontext a shrnutí", "## Dopady v praxi", "## Na co si dát pozor"]

    out = []
    for i, chunk in enumerate(parts):
        if not chunk:
            continue
        out.append(titles[min(i, len(titles)-1)])
        out.append("\n\n".join(chunk))
    return "\n\n".join(out)

# ===== LLM =====
def llm_rank_article(title: str, summary: str) -> int:
    user = f"""
Ohodnoť poutavost článku (1–5) pro odborný právnický blog.
Vrať pouze číslo 1–5.

Titulek: {title}
Anotace: {summary or "(bez anotace)"}
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=5,
    )
    add_usage(resp)
    txt = (resp.choices[0].message.content or "").strip()
    m = re.search(r"[1-5]", txt)
    return int(m.group()) if m else 2

def llm_generate_article(title: str, summary: str, source_url: str) -> str:
    user = f"""
Napiš česky srozumitelný článek pro širokou veřejnost (3–5 odstavců).
- Použij 1–2 mezititulky (Markdown "## " nebo "### ") – pomůže čitelnosti.
- Uveď 1× odkaz na původní zdroj: [zdroj]({source_url})
- Přirozeně vlož 1× kontextový odkaz na [právní poradenství Spring Walk](https://www.springwalk.cz/pravni-poradenstvi/) (bez prodejních frází).
- Piš věcně a drž se podkladu.

Podklad:
Titulek: {title}
Anotace/Perex: {summary or "(bez anotace)"}
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": user}],
        temperature=0.6,
        max_tokens=1280,  # požadovaný limit
    )
    add_usage(resp)
    md = (resp.choices[0].message.content or "").strip()
    md = ensure_springwalk_link(md)
    md = ensure_section_headings(md)  # vynutí 2–3 sekce s H2/H3, pokud chybí
    return md

def llm_generate_linkedin_posts(title: str, summary: str):
    user = f"""
Vytvoř 3 příspěvky na LinkedIn (každý 4–6 vět) k tématu níže.
Každý blok začni přesně:
"Společnost Spring Walk:"
"Jednatel (formální):"
"Jednatel (hravý):"
Bloky odděl třemi pomlčkami --- (na samostatném řádku).
Nepoužívej odrážky.

Titulek: {title}
Anotace: {summary or "(bez anotace)"}
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": user}],
        temperature=0.7,
        max_tokens=650,  # požadovaný limit
    )
    add_usage(resp)
    raw = (resp.choices[0].message.content or "").strip()
    blocks = [b.strip() for b in re.split(r'\n?---\n?', raw) if b.strip()]

    out = []
    labels = ["Společnost Spring Walk:", "Jednatel (formální):", "Jednatel (hravý):"]
    for i in range(3):
        b = blocks[i] if i < len(blocks) else labels[i]
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if lines:
            heading = lines[0].rstrip(":")
            body = " ".join(lines[1:]) if len(lines) > 1 else ""
        else:
            heading, body = labels[i].rstrip(":"), ""
        out.append(
            f'<div class="li-post"><div class="li-heading"><strong>{escape_html(heading)}</strong></div>'
            f'<div class="li-body">{escape_html(body)}</div></div>'
        )
    return out

# ===== HTML render =====
def render_index_html(articles, start_date: str, end_date: str) -> str:
    cards = []
    for a in articles:
        badge = f'<div class="badge">{escape_html(a["category"])}</div>' if a.get("category") else ""
        cards.append(f"""
<a class="card" href="{escape_html(a['file_name'])}">
  {badge}
  <h2>{escape_html(a['title'])}</h2>
  <div class="meta">{escape_html(a['source'])} — {a['published'].strftime('%d.%m.%Y')}</div>
</a>
""")
    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Právní novinky</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>Právní novinky ({escape_html(start_date)} – {escape_html(end_date)})</h1>
  <div class="grid">
    {''.join(cards) if cards else '<div class="meta">Zatím nic k zobrazení.</div>'}
  </div>
</div>
</body>
</html>"""

def render_post_html(a: dict) -> str:
    badge = f'<div class="badge">{escape_html(a["category"])}</div>' if a.get("category") else ""
    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{escape_html(a['title'])}</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div class="wrap">
  <a href="index.html" class="meta">← Zpět na přehled</a>
  {badge}
  <h1>{escape_html(a['title'])}</h1>
  <div class="meta">{escape_html(a['source'])} — {a['published'].strftime('%d.%m.%Y')}</div>

  <div class="article">
    {md_to_html(a['article_md'])}
  </div>

  <h2>Příspěvky na LinkedIn</h2>
  {''.join(a['linkedin_posts'])}
</div>
</body>
</html>"""

# ===== Hlavní běh =====
def fetch_articles():
    cutoff = datetime.now() - timedelta(days=DAYS_BACK)
    out = []
    seen = set()
    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        source_name = getattr(parsed.feed, "title", urlparse(feed_url).netloc)
        for e in parsed.entries:
            link = getattr(e, "link", "") or ""
            if not link or link in seen:
                continue
            pub = parse_pub_date(e)
            if pub and pub < cutoff:
                continue
            title = (getattr(e, "title", "") or "").strip()
            if not title:
                continue
            summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            # kategorie / téma
            topic = ""
            tags = getattr(e, "tags", None)
            if tags and isinstance(tags, list) and len(tags) and "term" in tags[0]:
                topic = tags[0]["term"] or ""
            else:
                topic = getattr(e, "category", "") or ""
            seen.add(link)
            out.append({
                "title": title,
                "link": link,
                "summary": summary,
                "published": pub or datetime.now(),
                "source": source_name,
                "category": topic,
            })
    return out

def main():
    articles = fetch_articles()

    # Ohodnotit poutavost a vybrat 3–5
    selected = []
    for a in articles:
        rating = llm_rank_article(a["title"], a["summary"])
        a["rating"] = rating
        if rating >= 3:
            selected.append(a)

    # Vygenerovat obsah
    for a in selected:
        a["article_md"] = llm_generate_article(a["title"], a["summary"], a["link"])
        a["linkedin_posts"] = llm_generate_linkedin_posts(a["title"], a["summary"])
        slug = slugify(a["title"])[:60]
        a["file_name"] = f"post_{slug}.html"

    # Časový rozsah na index
    if selected:
        start_date = min(x["published"] for x in selected).strftime("%d.%m.%Y")
        end_date = max(x["published"] for x in selected).strftime("%d.%m.%Y")
    else:
        today = datetime.now().strftime("%d.%m.%Y")
        start_date = end_date = today

    # Zápis indexu
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(render_index_html(selected, start_date, end_date))

    # Zápis detailů
    for a in selected:
        with open(a["file_name"], "w", encoding="utf-8") as f:
            f.write(render_post_html(a))

    # Orientační log
    print(f"Použito tokenů — input: {total_prompt_tokens}, output: {total_completion_tokens}")

if __name__ == "__main__":
    main()
