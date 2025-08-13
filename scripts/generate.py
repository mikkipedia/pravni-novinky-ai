# scripts/generate.py
# -----------------------------------------
# Právní novinky – RSS -> LLM -> statický web
# - stáhne články z RSS (posledních N dní)
# - ohodnotí poutavost 1–5 (LLM)
# - pro 3–5 vygeneruje článek + 3 LinkedIn posty
# - zapíše posts/*.html + index.html
# -----------------------------------------

import os
import re
import html
import feedparser
from datetime import datetime, timedelta
from dateutil import tz
from pathlib import Path
from typing import List, Dict, Any

# --- OpenAI oficiální knihovna (v1.x) ---
from openai import OpenAI

# ====== Konfigurace ======
FEEDS = [
    "https://www.epravo.cz/rss.php",
    "https://advokatnidenik.cz/feed/",
    "https://www.pravniprostor.cz/rss/aktuality",
]

OPENAI_MODEL = os.getenv("MODEL_NAME", "gpt-4o-mini")
DAYS_BACK = int(os.getenv("DAYS_BACK", "30"))

OUTPUT_DIR = Path(".")
POSTS_DIR = OUTPUT_DIR / "posts"
POSTS_DIR.mkdir(exist_ok=True)

API_KEY = os.environ.get("OPENAI_API_KEY")
if not API_KEY:
    raise RuntimeError("Chybí OPENAI_API_KEY v env. Přidej secret do GitHubu (Settings → Secrets → Actions).")

client = OpenAI(api_key=API_KEY)

# ====== Utility ======
def to_cz_date(dt: datetime) -> str:
    """Formátování na Europe/Prague (dd. mm. YYYY HH:MM)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz.UTC)
    return dt.astimezone(tz.gettz("Europe/Prague")).strftime("%-d. %-m. %Y %H:%M")

def parse_pubdate(entry) -> datetime | None:
    """Vrátí datetime z RSS položky (published/updated) v UTC, jinak None."""
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    try:
        return datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=tz.UTC)
    except Exception:
        return None

def slugify(text: str) -> str:
    """Jednoduchý slug bez diakritiky a speciálních znaků (pro názvy souborů)."""
    text = text.lower()
    replace_map = {
        ord('á'): 'a', ord('č'): 'c', ord('ď'): 'd', ord('é'): 'e', ord('ě'): 'e',
        ord('í'): 'i', ord('ň'): 'n', ord('ó'): 'o', ord('ř'): 'r', ord('š'): 's',
        ord('ť'): 't', ord('ú'): 'u', ord('ů'): 'u', ord('ý'): 'y', ord('ž'): 'z',
        ord('ä'): 'a', ord('ö'): 'o', ord('ü'): 'u'
    }
    text = text.translate(replace_map)
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text or "clanek"

def escape_html(s: str) -> str:
    return html.escape(s, quote=True)

# ====== Markdown → HTML (jednoduchý převod) ======
def md_links_to_html(text: str) -> str:
    """Převod [text](url) → <a href="url">text</a> (s target=_blank)."""
    return re.sub(
        r'\[([^\]]+)\]\((https?://[^\s)]+)\)',
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        text
    )

def md_to_html(txt: str) -> str:
    """
    Odstavce = oddělené prázdnou řádkou.
    Zachová h2/h3, převádí markdown odkazy na <a>.
    Pozn.: Odstavce zde ne-escapujeme, důvěřujeme výstupu LLM (pro prototyp OK).
    """
    txt = md_links_to_html(txt)
    parts = [p.strip() for p in re.split(r"\n\s*\n", txt.strip()) if p.strip()]
    html_pars: List[str] = []
    for p in parts:
        if p.startswith("## "):
            html_pars.append(f"<h2>{escape_html(p[3:].strip())}</h2>")
        elif p.startswith("### "):
            html_pars.append(f"<h3>{escape_html(p[4:].strip())}</h3>")
        else:
            html_pars.append(f"<p>{p}</p>")
    return "\n  ".join(html_pars)

# ====== LLM volání ======
def llm_classify_relevance(title: str, summary: str) -> int:
    """
    Vrátí integer 1–5 (1 = nezajímavé, 5 = průlomové).
    """
    prompt = f"""
Jsi právní analytik. Ohodnoť POUTAVOST pro odborný právnický blog na škále 1–5:
1 = drobná aktualita bez významu,
2 = okrajové,
3 = relevantní pro část čtenářů,
4 = významné (dopad/precedens),
5 = průlomové (zásadní novela/ÚS/SDEU).

Vrať pouze číslo 1–5, nic jiného.

Titulek: {title}
Anotace: {summary or "(bez anotace)"}
    """.strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Odpovídej pouze číslem 1–5."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
    )
    text = (resp.choices[0].message.content or "").strip()
    try:
        n = int(re.findall(r"[1-5]", text)[0])
        return n
    except Exception:
        return 2  # konzervativní default

def llm_generate_article(title: str, summary: str, source_url: str) -> str:
    """
    Vygeneruje 3–5 odstavců srozumitelného článku pro širokou veřejnost,
    s 1× odkazem na zdroj a 1× odkazem na Spring Walk poradenství.
    """
    user = f"""
Napiš česky srozumitelný a snadno čitelný článek pro širokou veřejnost (3–5 odstavců).
Styl: jasný, kratší věty, bez žargonu, bez reklamy a bez výzev „kontaktujte nás“.

POVINNÉ:
- Použij 1–2 mezititulky (Markdown „## “).
- V textu přirozeně uveď 1× odkaz na původní zdroj ve formátu [zdroj]({source_url}).
- V závěru nebo v části „Co z toho plyne“ uveď 1× odkaz na [právní poradenství Spring Walk](https://www.springwalk.cz/pravni-poradenstvi/).
- Drž se faktů z podkladu, nic si nevymýšlej.

Podklad:
Titulek: {title}
Anotace/Perex: {summary or "(bez anotace)"}

Na úplný konec přidej větu: „Zpracováno z veřejných zdrojů.“
    """.strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Jsi zkušený právní copywriter. Píšeš česky, srozumitelně a bez reklamy."},
            {"role": "user", "content": user}
        ],
        temperature=0.5,
        max_tokens=900,
    )
    return (resp.choices[0].message.content or "").strip()

def llm_generate_linkedin_posts(title: str, summary: str) -> List[str]:
    """
    Vrátí 3 varianty krátkých postů (2–3 věty) s hlavičkou „od koho je“:
    1) Společnost Spring Walk
    2) Jednatel (formální)
    3) Jednatel (hravý)
    """
    user = f"""
Vytvoř 3 různé krátké příspěvky na LinkedIn (česky), každý 2–3 věty, k tématu níže.
Každý blok začni NADPISEM „Společnost Spring Walk:“ / „Jednatel (formální):“ / „Jednatel (hravý):“ (v tomto přesném znění), poté text.
Bez reklamy a bez výzev „kontaktujte nás“.

Podklad:
Titulek: {title}
Anotace/Perex: {summary or "(bez anotace)"}

Formát výstupu:
---
Společnost Spring Walk:
<2–3 věty>
---
Jednatel (formální):
<2–3 věty>
---
Jednatel (hravý):
<2–3 věty>
---
    """.strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Jsi content specialista pro LinkedIn. Píšeš česky, věcně a přívětivě."},
            {"role": "user", "content": user}
        ],
        temperature=0.7,
        max_tokens=600,
    )
    raw = (resp.choices[0].message.content or "").strip()

    # Rozdělit podle '---' bloků a sestavit HTML s tučnou hlavičkou
    blocks = [b.strip() for b in re.split(r'\n?---\n?', raw) if b.strip()]
    posts: List[str] = []
    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if not lines:
            continue
        heading = lines[0]
        body = " ".join(lines[1:]) if len(lines) > 1 else ""
        posts.append(f"<strong>{escape_html(heading)}</strong> {escape_html(body)}")
    # jistota, že jsou přesně 3
    if len(posts) >= 3:
        return posts[:3]
    if posts:
        return posts + [""] * (3 - len(posts))
    return ["", "", ""]

# ====== HTML šablony ======
def render_post_html(title: str, article_html: str, posts: List[str], rating: int, source_url: str, pub_date_str: str) -> str:
    esc_title = escape_html(title)
    esc_url = escape_html(source_url or "#")
    items = "\n".join(f"      <li>{p}</li>" for p in posts)  # posts už obsahují <strong> + escapovaný text
    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
  <meta charset="UTF-8" />
  <title>{esc_title}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; max-width: 840px; margin: 2rem auto; padding: 0 1rem; line-height: 1.6; }}
    h1 {{ line-height: 1.2; }}
    h2 {{ margin-top: 1.5rem; }}
    .meta {{ color:#666; font-size: 0.9rem; margin-bottom: 1rem; }}
    .back {{ margin-top: 2rem; }}
    ul {{ padding-left: 1.2rem; }}
    .pill {{ display:inline-block; padding:.15rem .5rem; border:1px solid #ccc; border-radius:999px; font-size:.8rem; color:#333; }}
    hr {{ border:none; border-top:1px solid #eee; margin:1.5rem 0; }}
    a {{ color:#0b57d0; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <a class="pill" href="../index.html">← Přehled</a>
  <h1>{esc_title}</h1>
  <div class="meta">
    Poutavost: <strong>{rating}/5</strong> &nbsp;|&nbsp; Zdroj: <a href="{esc_url}" target="_blank" rel="noopener">odkaz</a> &nbsp;|&nbsp; Publikováno: {escape_html(pub_date_str)}
  </div>
  {article_html}

  <hr />
  <h2>Tipy na příspěvky na LinkedIn</h2>
  <ul>
{items}
  </ul>

  <p class="back"><a href="../index.html">← Zpět na přehled</a></p>
</body>
</html>
"""

def render_index_html(items: List[Dict[str, Any]]) -> str:
    """
    items: list dicts {title, href, rating, source, pub_date_str}
    """
    li = []
    for it in items:
        li.append(f'''    <li>
      <a href="{escape_html(it["href"])}">{escape_html(it["title"])}</a>
      <div class="meta">Poutavost: <strong>{it["rating"]}/5</strong> &nbsp;|&nbsp; Zdroj: {escape_html(it["source"])} &nbsp;|&nbsp; Publikováno: {escape_html(it["pub_date_str"])}</div>
    </li>''')
    lis = "\n".join(li) if li else "    <li>Zatím nic k zobrazení.</li>"
    updated = to_cz_date(datetime.now(tz.UTC))
    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
  <meta charset="UTF-8" />
  <title>Právní novinky – AI generátor</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.6; }}
    h1 {{ line-height: 1.2; }}
    .meta {{ color:#666; font-size: 0.9rem; }}
    ul.posts {{ list-style: none; padding-left: 0; }}
    ul.posts > li {{ margin: 1rem 0 1.25rem; }}
    a {{ color:#0b57d0; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <h1>Právní novinky – AI generované články</h1>
  <p>Tento web automaticky sbírá české právní novinky (posledních {DAYS_BACK} dní), hodnotí jejich poutavost (1–5) a pro relevantní témata (3–5) generuje stručné články a 3 tipy na příspěvky na LinkedIn. Bez reklamy, pouze informativní publicita.</p>
  <p class="meta">Poslední aktualizace: {escape_html(updated)}</p>

  <h2>Seznam článků</h2>
  <ul class="posts">
{lis}
  </ul>
</body>
</html>
"""

# ====== Hlavní běh ======
def main():
    cutoff = datetime.now(tz.UTC) - timedelta(days=DAYS_BACK)
    seen_links = set()
    collected: List[Dict[str, Any]] = []

    # 1) Sběr z RSS
    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        source_name = (getattr(feed, "feed", None) and getattr(feed.feed, "title", None)) or feed_url
        for e in feed.entries:
            link = getattr(e, "link", "") or ""
            if not link or link in seen_links:
                continue
            pub_dt = parse_pubdate(e)
            if pub_dt and pub_dt < cutoff:
                continue

            title = (getattr(e, "title", "") or "").strip()
            summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
            if not title:
                continue

            seen_links.add(link)
            collected.append({
                "title": title,
                "summary": summary,
                "link": link,
                "source": source_name,
                "pub_dt": pub_dt,
            })

    # 2) LLM klasifikace relevance
    selected: List[Dict[str, Any]] = []
    for item in collected:
        rating = llm_classify_relevance(item["title"], item["summary"])
        item["rating"] = rating
        if rating >= 3:
            selected.append(item)

    # 3) Generování článků + LinkedIn postů
    index_items: List[Dict[str, Any]] = []
    for item in selected:
        title = item["title"]
        summary = item["summary"]
        link = item["link"]
        rating = item["rating"]
        pub_dt = item["pub_dt"]
        pub_date_str = to_cz_date(pub_dt) if pub_dt else "neznámo"

        article_md = llm_generate_article(title, summary, link)
        article_html = md_to_html(article_md)
        posts = llm_generate_linkedin_posts(title, summary)

        slug = slugify(title)[:60]
        fn = POSTS_DIR / f"{slug}.html"
        html_out = render_post_html(title, article_html, posts, rating, link, pub_date_str)
        fn.write_text(html_out, encoding="utf-8")

        index_items.append({
            "title": title,
            "href": f"posts/{fn.name}",
            "rating": rating,
            "source": item["source"],
            "pub_date": pub_dt or datetime.min.replace(tzinfo=tz.UTC),
            "pub_date_str": pub_date_str,
        })

    # 4) Index – novější nahoře
    index_items.sort(key=lambda x: x["pub_date"], reverse=True)
    index_html = render_index_html(index_items)
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

if __name__ == "__main__":
    main()
