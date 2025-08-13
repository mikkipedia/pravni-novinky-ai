# scripts/generate.py
# -----------------------------------------
# Právní novinky – RSS -> LLM -> statický web (dark, card design)
# - stáhne články z RSS (posledních N dní)
# - ohodnotí poutavost 1–5 (LLM)
# - pro 3–5 vygeneruje článek + 3 LinkedIn posty
# - zapíše cost.txt + cost.json (tokeny + odhad ceny v USD i CZK)
# - zapíše posts/*.html + index.html (dark theme + cards + cost line)
# -----------------------------------------

import os
import re
import html
import json
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

# Cena (USD za 1M tokenů) – lze přepsat ve workflow env
INPUT_PRICE_PER_MTOK = float(os.getenv("INPUT_PRICE_USD_PER_MTOK", "0.15"))
OUTPUT_PRICE_PER_MTOK = float(os.getenv("OUTPUT_PRICE_USD_PER_MTOK", "0.60"))
USD_TO_CZK = float(os.getenv("USD_TO_CZK", "24.5"))  # kurz pro převod na Kč

OUTPUT_DIR = Path(".")
POSTS_DIR = OUTPUT_DIR / "posts"
POSTS_DIR.mkdir(exist_ok=True)

API_KEY = os.environ.get("OPENAI_API_KEY")
if not API_KEY:
    raise RuntimeError("Chybí OPENAI_API_KEY v env. Přidej secret do GitHubu (Settings → Secrets → Actions).")

client = OpenAI(api_key=API_KEY)

# ====== Token/metrika ======
TOK_IN = 0
TOK_OUT = 0

def add_usage(resp):
    """Sečte usage tokeny z response (bezpečně)."""
    global TOK_IN, TOK_OUT
    try:
        u = resp.usage
        TOK_IN += int(getattr(u, "prompt_tokens", getattr(u, "input_tokens", 0)) or 0)
        TOK_OUT += int(getattr(u, "completion_tokens", getattr(u, "output_tokens", 0)) or 0)
    except Exception:
        pass

# ====== Utility ======
def to_cz_date(dt: datetime) -> str:
    """Formátování na Europe/Prague (dd. mm. YYYY HH:MM)."""
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz.UTC)
    if not dt:
        return "neznámo"
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
    """Vrátí integer 1–5 (1 = nezajímavé, 5 = průlomové)."""
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
    add_usage(resp)
    text = (resp.choices[0].message.content or "").strip()
    try:
        n = int(re.findall(r"[1-5]", text)[0])
        return n
    except Exception:
        return 2  # konzervativní default

def llm_generate_article(title: str, summary: str, source_url: str) -> str:
    """
    Vygeneruje 3–5 odstavců srozumitelného článku pro širokou veřejnost,
    s 1× odkazem na zdroj a 1× nenásilným kontextovým odkazem na Spring Walk poradenství.
    """
    user = f"""
Napiš česky srozumitelný a snadno čitelný článek pro širokou veřejnost (3–5 odstavců).
Styl: jasný, kratší věty, bez žargonu, bez reklamy a bez výzev „kontaktujte nás“.

POVINNÉ:
- Použij 1–2 mezititulky (Markdown „## “).
- V textu přirozeně uveď 1× odkaz na původní zdroj ve formátu [zdroj]({source_url}).
- VLOŽ nenásilně a pouze 1× **kontextový** odkaz na [právní poradenství Spring Walk](https://www.springwalk.cz/pravni-poradenstvi/). Zasaď ho do věty tak, aby působil organicky (např. při vysvětlení dopadů nebo doporučení, bez prodejních frází).
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
        max_tokens=1200,
    )
    add_usage(resp)
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
    add_usage(resp)
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
    if len(posts) >= 3:
        return posts[:3]
    if posts:
        return posts + [""] * (3 - len(posts))
    return ["", "", ""]

# ====== HTML šablony (dark theme) ======
BASE_CSS = """
  :root {
    --bg: #0b0f14;
    --panel: #151a21;
    --text: #e6e6e6;
    --muted: #a8b0bb;
    --accent: #8ab4f8;
    --success: #7dd3a7;
    --danger: #ff6b6b;
    --radius: 16px;
    --shadow: 0 10px 24px rgba(0,0,0,.45), 0 2px 6px rgba(0,0,0,.3);
    --shadow-hover: 0 18px 36px rgba(0,0,0,.55), 0 6px 14px rgba(0,0,0,.35);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Inter, Arial, sans-serif;
    line-height: 1.65;
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 32px 20px 56px; }
  header { margin-bottom: 24px; }
  h1, h2, h3 {
    text-transform: uppercase;
    letter-spacing: .04em;
    font-weight: 800;
  }
  h1 { font-size: 28px; margin: 0 0 10px; }
  h2 { font-size: 18px; margin: 22px 0 8px; }
  h3 { font-size: 16px; margin: 18px 0 6px; }
  .meta {
    color: var(--muted);
    font-family: Georgia, 'Times New Roman', serif;
    font-style: italic;
    font-size: .95rem;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 18px;
  }
  .card {
    display: block;
    background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.0));
    background-color: var(--panel);
    border-radius: var(--radius);
    padding: 18px 18px 16px;
    box-shadow: var(--shadow);
    border: 1px solid rgba(255,255,255,.06);
    transform: translateY(0);
    transition: transform .25s ease, box-shadow .25s ease, border-color .25s ease;
  }
  .card:hover {
    transform: translateY(-6px);
    box-shadow: var(--shadow-hover);
    border-color: rgba(255,255,255,.12);
  }
  .pill {
    display:inline-block; padding:.2rem .6rem; border:1px solid rgba(255,255,255,.2);
    border-radius:999px; font-size:.8rem; color:var(--muted);
  }
  .footer { margin-top: 28px; color: var(--muted); }
  .article {
    background-color: var(--panel);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    border: 1px solid rgba(255,255,255,.06);
    padding: 22px 22px;
  }
  .article p { margin: 12px 0; }
  hr { border: none; border-top: 1px solid rgba(255,255,255,.08); margin: 24px 0; }
  ul { margin: 10px 0 0 20px; }
"""

def render_post_html(
    title: str,
    article_html: str,
    posts: List[str],
    rating: int,
    source_url: str,
    pub_date_str: str,
    cost_line: str = ""
) -> str:
    esc_title = escape_html(title)
    esc_url = escape_html(source_url or "#")
    items = "\n".join(f"      <li>{p}</li>" for p in posts)  # posts už obsahují <strong> + escapovaný text
    footer = f'<div class="footer meta">{escape_html(cost_line)}</div>' if cost_line else ""
    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
  <meta charset="UTF-8" />
  <title>{esc_title}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="wrap">
    <header>
      <a class="pill" href="../index.html">← Přehled</a>
      <h1>{esc_title}</h1>
      <div class="meta">Poutavost: <strong>{rating}/5</strong> · Zdroj: <a href="{esc_url}" target="_blank" rel="noopener">odkaz</a> · Publikováno: {escape_html(pub_date_str)}</div>
    </header>

    <article class="article">
      {article_html}

      <hr />
      <h2>Tipy na příspěvky na LinkedIn</h2>
      <ul>
{items}
      </ul>
    </article>

    {footer}
  </div>
</body>
</html>
"""

def render_index_html(items: List[Dict[str, Any]], cost_line: str = "") -> str:
    """
    items: list dicts {title, href, rating, source, pub_date_str}
    cost_line: volitelný řádek s náklady (vloží se do patičky)
    """
    cards = []
    for it in items:
        cards.append(f'''  <a class="card" href="{escape_html(it["href"])}">
      <div class="meta">{escape_html(it["source"])} · {escape_html(it["pub_date_str"])}</div>
      <h2>{escape_html(it["title"])}</h2>
      <div class="meta">Poutavost: <strong>{it["rating"]}/5</strong></div>
    </a>''')
    grid = "\n".join(cards) if cards else '<div class="meta">Zatím nic k zobrazení.</div>'
    footer = f'<div class="footer">{escape_html(cost_line)}</div>' if cost_line else ""
    updated = to_cz_date(datetime.now(tz.UTC))
    return f"""<!DOCTYPE html>
<html lang="cs">
<head>
  <meta charset="UTF-8" />
  <title>Právní novinky – AI generátor</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>{BASE_CSS}</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>Právní novinky – AI generované články</h1>
      <div class="meta">Poslední aktualizace: {escape_html(updated)}</div>
    </header>

    <div class="grid">
{grid}
    </div>

    {footer}
  </div>
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

    # 3) Generování článků + LinkedIn postů (zatím jen do paměti)
    render_queue = []
    index_items: List[Dict[str, Any]] = []

    for item in selected:
        title = item["title"]
        summary = item["summary"]
        link = item["link"]
        rating = item["rating"]
        pub_dt = item["pub_dt"]
        pub_date_str = to_cz_date(pub_dt)

        article_md = llm_generate_article(title, summary, link)
        article_html = md_to_html(article_md)
        posts = llm_generate_linkedin_posts(title, summary)

        slug = slugify(title)[:60]
        fn = POSTS_DIR / f"{slug}.html"

        render_queue.append({
            "filepath": fn,
            "title": title,
            "article_html": article_html,
            "posts": posts,
            "rating": rating,
            "link": link,
            "pub_date_str": pub_date_str,
        })

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

    # 5) Náklady: spočítat a zapsat cost.txt + cost.json (USD i CZK)
    input_tokens = TOK_IN
    output_tokens = TOK_OUT
    cost_usd = (input_tokens * (INPUT_PRICE_PER_MTOK / 1_000_000.0)) + \
               (output_tokens * (OUTPUT_PRICE_PER_MTOK / 1_000_000.0))
    cost_czk = cost_usd * USD_TO_CZK

    cost_info = {
        "timestamp": datetime.now(tz.UTC).isoformat(),
        "model": OPENAI_MODEL,
        "days_back": DAYS_BACK,
        "feeds": FEEDS,
        "items_collected": len(collected),
        "items_selected": len(selected),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "input_price_usd_per_mtok": INPUT_PRICE_PER_MTOK,
        "output_price_usd_per_mtok": OUTPUT_PRICE_PER_MTOK,
        "cost_usd": round(cost_usd, 6),
        "cost_czk": round(cost_czk, 4),
        "usd_to_czk_rate": USD_TO_CZK,
    }
    (OUTPUT_DIR / "cost.json").write_text(json.dumps(cost_info, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUTPUT_DIR / "cost.txt").write_text(
        (
            f"Model: {OPENAI_MODEL}\n"
            f"Položky: {len(collected)} (vybráno {len(selected)})\n"
            f"Input tokens:  {input_tokens}\n"
            f"Output tokens: {output_tokens}\n"
            f"Cena (odhad):  ${cost_usd:.4f}  (~{cost_czk:.2f} Kč při kurzu {USD_TO_CZK})\n"
            f"Ceník: input ${INPUT_PRICE_PER_MTOK}/1M, output ${OUTPUT_PRICE_PER_MTOK}/1M\n"
        ),
        encoding="utf-8"
    )
    cost_line = f"Odhad nákladů posledního běhu: ${cost_usd:.4f} (~{cost_czk:.2f} Kč) · input {input_tokens} tok., output {output_tokens} tok., model {OPENAI_MODEL}"

    # 5b) Zapiš jednotlivé články s patičkou (cost_line)
    for it in render_queue:
        html_out = render_post_html(
            it["title"],
            it["article_html"],
            it["posts"],
            it["rating"],
            it["link"],
            it["pub_date_str"],
            cost_line=cost_line
        )
        it["filepath"].write_text(html_out, encoding="utf-8")

    # 6) Index s cost_line
    index_html = render_index_html(index_items, cost_line=cost_line)
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

if __name__ == "__main__":
    main()
