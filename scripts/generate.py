import os
import re
import html
import json
import feedparser
from datetime import datetime, timedelta
from dateutil import tz
from pathlib import Path

# --- OpenAI (oficiální knihovna 1.x) ---
from openai import OpenAI
OPENAI_MODEL = os.getenv("MODEL_NAME", "gpt-4o-mini")

API_KEY = os.environ.get("OPENAI_API_KEY")
if not API_KEY:
    raise RuntimeError("Chybí OPENAI_API_KEY v env. Přidej secret do GitHubu.")

client = OpenAI(api_key=API_KEY)

# --- Konfigurace ---
FEEDS = [
    "https://www.epravo.cz/rss.php",
    "https://advokatnidenik.cz/feed/",
    "https://www.pravniprostor.cz/rss/aktuality",
]
DAYS_BACK = int(os.getenv("DAYS_BACK", "30"))
OUTPUT_DIR = Path(".")
POSTS_DIR = OUTPUT_DIR / "posts"
POSTS_DIR.mkdir(exist_ok=True)

# --- Utility ---
def to_cz_date(dt: datetime) -> str:
    """Formátování data pro CZ."""
    return dt.astimezone(tz.gettz("Europe/Prague")).strftime("%-d. %-m. %Y %H:%M")

def parse_pubdate(entry):
    """Vrátí datetime z RSS položky, pokud jde (jinak None)."""
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    try:
        return datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, tzinfo=tz.UTC)
    except Exception:
        return None

def slugify(text: str) -> str:
    """Jednoduchý slug (bez diakritiky) – pro prototyp postačí."""
    text = text.lower()
    # Odstranit diakritiku hrubou náhradou
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

# --- LLM volání ---
def llm_classify_relevance(title: str, summary: str) -> int:
    """
    Vrátí integer 1–5 (1 = nezajímavé, 5 = průlomové).
    """
    prompt = f"""
Jsi právní analytik. Ohodnoť POUTAVOST pro odborný právnický blog, škálou 1–5.
Kritéria:
1 = drobná aktualita bez dopadu,
2 = okrajové,
3 = relevantní pro část čtenářů,
4 = významné (dopad/precedens),
5 = průlomové (zásadní novela/ÚS/SDEU/Judikát s dopady).

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
    text = resp.choices[0].message.content.strip()
    try:
        n = int(re.findall(r"[1-5]", text)[0])
        return n
    except Exception:
        return 2  # konzervativní default

def llm_generate_article(title: str, summary: str, source_url: str) -> str:
    """
    Vygeneruje 3–5 odstavců článku (CZ, odborně, bez reklamy).
    """
    user = f"""
Napiš česky souvislý odborný článek pro blog advokátní kanceláře (3–5 odstavců).
Styl: věcný, srozumitelný, bez reklamy, bez výzev ke kontaktu. Vysvětli, proč je novinka důležitá v praxi.
Drž se faktů z podkladu, nic si nevymýšlej.

Podklad:
Titulek: {title}
Anotace/Perex: {summary or "(bez anotace)"}

Uveď vhodný stručný mezititulek(ky), ale žádné „závěry“ typu promo.
Na závěr uveď větu: "Zpracováno z veřejných zdrojů." (bez odkazu).
    """.strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Jsi seniorní právní copywriter. Píšeš česky, věcně a bez reklamy."},
            {"role": "user", "content": user}
        ],
        temperature=0.5,
        max_tokens=900,
    )
    return resp.choices[0].message.content.strip()

def llm_generate_linkedin_posts(title: str, summary: str) -> list:
    """
    Vrátí 3 varianty krátkých postů (2–3 věty), různé úhly pohledu.
    """
    user = f"""
Vytvoř 3 různé krátké příspěvky na LinkedIn (česky), každý 2–3 věty.
Tón: profesionální, bez reklamy, bez výzev typu "kontaktujte nás".
Varianty:
1) neutrální shrnutí,
2) edukativní (co to znamená pro praxi),
3) mírně osobní (náhled „co sledovat dál“).

Podklad:
Titulek: {title}
Anotace/Perex: {summary or "(bez anotace)"}

Vrať přesně 3 odrážky "-" (pomlčka mezera text).
    """.strip()

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": "Jsi content specialista pro LinkedIn. Píšeš česky, věcně."},
            {"role": "user", "content": user}
        ],
        temperature=0.7,
        max_tokens=500,
    )
    raw = resp.choices[0].message.content.strip()
    # Parsovat odrážky začínající "- "
    posts = [re.sub(r"^-+\s*", "", line).strip() for line in raw.splitlines() if line.strip().startswith("-")]
    # omezit na 3
    return posts[:3] if posts else [raw]

# --- HTML výstup ---
def render_post_html(title: str, article_html: str, posts: list, rating: int, source_url: str, pub_date_str: str) -> str:
    esc_title = escape_html(title)
    esc_url = escape_html(source_url or "#")
    items = "\n".join(f"      <li>{escape_html(p)}</li>" for p in posts)
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

def render_index_html(items: list) -> str:
    """
    items: list dicts {title, href, rating, source, pub_date}
    """
    li = []
    for it in items:
        li.append(f'''    <li>
      <a href="{escape_html(it["href"])}">{escape_html(it["title"])}</a>
      <div class="meta">Poutavost: <strong>{it["rating"]}/5</strong> &nbsp;|&nbsp; Zdroj: {escape_html(it["source"])} &nbsp;|&nbsp; Publikováno: {escape_html(it["pub_date"])}</div>
    </li>''')
    lis = "\n".join(li)
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
    .badge {{ display:inline-block; padding:.15rem .5rem; border:1px solid #ccc; border-radius:999px; font-size:.8rem; color:#333; }}
  </style>
</head>
<body>
  <h1>Právní novinky – AI generované články</h1>
  <p>Tento web automaticky sbírá české právní novinky (posledních {DAYS_BACK} dní), hodnotí jejich poutavost (1–5) a pro relevantní témata (3–5) generuje stručné články a 3 tipy na příspěvky na LinkedIn. Bez reklamy, pouze informativní publicita.</p>
  <p class="meta">Poslední aktualizace: {escape_html(updated)}</p>

  <h2>Seznam článků</h2>
  <ul class="posts">
{lis if lis else "    <li>Zatím nic k zobrazení.</li>"}
  </ul>
</body>
</html>
"""

# --- Hlavní běh ---
def main():
    cutoff = datetime.now(tz.UTC) - timedelta(days=DAYS_BACK)
    seen_links = set()
    collected = []

    for feed_url in FEEDS:
        feed = feedparser.parse(feed_url)
        source_name = feed.feed.title if hasattr(feed, "feed") and hasattr(feed.feed, "title") else feed_url
        for e in feed.entries:
            link = getattr(e, "link", "")
            if not link or link in seen_links:
                continue
            pub_dt = parse_pubdate(e)
            if pub_dt and pub_dt < cutoff:
                continue

            title = getattr(e, "title", "").strip()
            summary = getattr(e, "summary", "") or getattr(e, "description", "")
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

    # Relevance scoring
    selected = []
    for item in collected:
        rating = llm_classify_relevance(item["title"], item["summary"])
        item["rating"] = rating
        if rating >= 3:
            selected.append(item)

    # Generování a zápis
    index_items = []
    for item in selected:
        title = item["title"]
        summary = item["summary"]
        link = item["link"]
        rating = item["rating"]
        pub_str = to_cz_date(item["pub_dt"]) if item["pub_dt"] else "neznámo"

        article_md = llm_generate_article(title, summary, link)
        # Prostý převod odstavců na <p>; ponecháme h2/h3 pokud model vytvořil
        # Bezpečně escapujeme, ale povolíme základní nadpisy a odstavce.
        # Zde pro jednoduchost: nahradíme prázdné řádky za <p> bloky.
        def md_to_html(txt: str) -> str:
            # jednoduché: odstavce = dvojitý newline
            parts = [p.strip() for p in re.split(r"\n\s*\n", txt.strip()) if p.strip()]
            html_pars = []
            for p in parts:
                # zachovej h2/h3, jinak <p>
                if p.startswith("## "):
                    html_pars.append(f"<h2>{escape_html(p[3:].strip())}</h2>")
                elif p.startswith("### "):
                    html_pars.append(f"<h3>{escape_html(p[4:].strip())}</h3>")
                else:
                    # zachovej základní interpunkci, escapuj
                    html_pars.append(f"<p>{escape_html(p)}</p>")
            return "\n  ".join(html_pars)

        article_html = md_to_html(article_md)
        posts = llm_generate_linkedin_posts(title, summary)

        slug = slugify(title)[:60]
        fn = POSTS_DIR / f"{slug}.html"
        html_out = render_post_html(title, article_html, posts, rating, link, pub_str)
        fn.write_text(html_out, encoding="utf-8")

        index_items.append({
            "title": title,
            "href": f"posts/{fn.name}",
            "rating": rating,
            "source": item["source"],
            "pub_date": pub_str,
        })

    # Seřadit index (novější nahoře)
    index_items.sort(key=lambda x: x["pub_date"], reverse=True)
    index_html = render_index_html(index_items)
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

if __name__ == "__main__":
    main()

