# scripts/generate.py
# -----------------------------------------
# RSS -> LLM -> statický web (light / Forbes-like)
# - sbírá české právní novinky z RSS (posledních N dní)
# - ohodnotí poutavost 1–5 (LLM)
# - pro 3–5 vygeneruje článek + 3 LinkedIn posty
# - článek VŽDY rozdělí do 2–3 sekcí s H2/H3 nadpisy
# - používá externí CSS: assets/style.css
# - délky: článek max_tokens=1280, LI posty max_tokens=650
# - dole na indexu zobrazuje odhad nákladů (model) + reálné usage z API
# -----------------------------------------

from datetime import datetime, timedelta
from html import escape as escape_html
from urllib.parse import urlparse
import os
import re

import feedparser
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

# ===== Ceník a odhadové parametry (dle README) =====
# Jednotkové ceny (USD / token)
INPUT_PRICE_USD = float(os.getenv("INPUT_PRICE_USD", str(0.15 / 1_000_000)))
OUTPUT_PRICE_USD = float(os.getenv("OUTPUT_PRICE_USD", str(0.60 / 1_000_000)))
USD_TO_CZK = float(os.getenv("USD_TO_CZK", "23.5"))

# Průměrné tokeny (lze ladit přes env)
IN_CLS = int(os.getenv("IN_CLS", "300"))      # klasifikace na položku (input)
OUT_CLS = int(os.getenv("OUT_CLS", "1"))      # klasifikace na položku (output)
IN_BLOG = int(os.getenv("IN_BLOG", "350"))    # 1 vybraný článek (input)
OUT_BLOG = int(os.getenv("OUT_BLOG", "700"))  # 1 vybraný článek (output)
IN_LI = int(os.getenv("IN_LI", "300"))        # 3 LI posty dohromady (input)
OUT_LI = int(os.getenv("OUT_LI", "220"))      # 3 LI posty dohromady (output)

# ===== Usage tracking (reálné měření) =====
total_prompt_tokens = 0
total_completion_tokens = 0


def add_usage(resp):
    """Bezpečně přičti počty tokenů z odpovědi API (různé názvy dle modelu)."""
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
    """Vrať datetime z published/updated, pokud je k dispozici."""
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if not t:
        return None
    try:
        return datetime(t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)
    except Exception:
        return None


def slugify(text: str) -> str:
    """Konzervativní slug pro název souboru (CZ diakritika -> ASCII)."""
    text = text.lower()
    cz = {
        ord("á"): "a", ord("č"): "c", ord("ď"): "d", ord("é"): "e", ord("ě"): "e",
        ord("í"): "i", ord("ň"): "n", ord("ó"): "o", ord("ř"): "r", ord("š"): "s",
        ord("ť"): "t", ord("ú"): "u", ord("ů"): "u", ord("ý"): "y", ord("ž"): "z",
        ord("ä"): "a", ord("ö"): "o", ord("ü"): "u",
    }
    text = text.translate(cz)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "clanek"


def md_links_to_html(text: str) -> str:
    """Konvertuj Markdown odkazy [text](url) na HTML <a>."""
    return re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        text,
    )


def md_to_html(txt: str) -> str:
    """Základní převod Markdownu: odstavce a H2/H3."""
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
    """Zaruč jeden kontextový odkaz na Spring Walk, pokud v textu chybí."""
    if "springwalk.cz" not in md.lower():
        md += (
            "\n\nDalší informace nabízí "
            "[právní poradenství Spring Walk]"
            "(https://www.springwalk.cz/pravni-poradenstvi/)."
        )
    return md


def ensure_section_headings(md: str) -> str:
    """
    Pokud článek obsahuje < 2 nadpisy (##/###), rozděl text na 2–3 tematické sekce
    a vlož generické H2 nadpisy. Odkazy zůstanou zachované.
    """
    if len(re.findall(r"^\s*##\s+|^\s*###\s+", md, flags=re.MULTILINE)) >= 2:
        return md

    paras = [p.strip() for p in re.split(r"\n\s*\n", md.strip()) if p.strip()]
    if not paras:
        return md

    if len(paras) <= 3:
        split = max(1, len(paras) // 2)  # 2 sekce
        parts = [paras[:split], paras[split:]]
        titles = ["## Co se stalo", "## Co z toho plyne"]
    else:
        third = max(1, len(paras) // 3)  # 3 sekce
        parts = [paras[:third], paras[third:2 * third], paras[2 * third:]]
        titles = ["## Kontext a shrnutí", "## Dopady v praxi", "## Na co si dát pozor"]

    out = []
    for i, chunk in enumerate(parts):
        if not chunk:
            continue
        out.append(titles[min(i, len(titles) - 1)])
        out.append("\n\n".join(chunk))
    return "\n\n".join(out)


# ===== LLM =====
def llm_rank_article(title: str, summary: str) -> int:
    """Vrať skóre poutavosti 1–5 na základě titulku a anotace."""
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
    """Vytvoř článek v Markdownu, následně zajisti sekce a odkaz na Spring Walk."""
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
        max_tokens=1280,
    )
    add_usage(resp)
    md = (resp.choices[0].message.content or "").strip()
    md = ensure_springwalk_link(md)
    md = ensure_section_headings(md)
    return md


def llm_generate_linkedin_posts(title: str, summary: str):
    """Vytvoř 3 bloky textu pro LinkedIn a vrať je jako HTML snippet."""
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
        max_tokens=650,
    )
    add_usage(resp)

    raw = (resp.choices[0].message.content or "").strip()
    blocks = [b.strip() for b in re.split(r"\n?---\n?", raw) if b.strip()]

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
def render_index_html(articles, start_date: str, end_date: str, usage_real: dict, usage_est: dict) -> str:
    """Vytvoř HTML přehledu článků + patička s odhadem a měřením."""
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

    footer = f"""
<div class="footer">
  <div><strong>Odhad (model)</strong></div>
  <div>Input: <strong>{int(usage_est['input_tokens']):,}</strong>, Output: <strong>{int(usage_est['output_tokens']):,}</strong></div>
  <div>Odhad ceny: <strong>${usage_est['cost_usd']:.4f}</strong> (~{usage_est['cost_czk']:.2f} Kč)</div>

  <div style="margin-top:10px;"><strong>Měřeno (API)</strong></div>
  <div>Tokeny — input: <strong>{usage_real['prompt_tokens']:,}</strong>, output: <strong>{usage_real['completion_tokens']:,}</strong>, celkem: <strong>{usage_real['total_tokens']:,}</strong></div>
  <div class="meta">Pozn.: Odhad vychází z průměrů na položku a může se lišit od skutečného účtování.</div>
</div>
"""

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
  {footer}
</div>
</body>
</html>"""


def render_post_html(a: dict) -> str:
    """Vytvoř HTML detailu článku."""
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
    """Načti položky z RSS, filtruj duplicitní a staré záznamy, obohať o metadata."""
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


def estimate_costs(n_items: int, n_selected: int) -> dict:
    """Odhad tokenů a ceny dle README metodiky."""
    input_tokens = n_items * IN_CLS + n_selected * (IN_BLOG + IN_LI)
    output_tokens = n_items * OUT_CLS + n_selected * (OUT_BLOG + OUT_LI)
    cost_usd = input_tokens * INPUT_PRICE_USD + output_tokens * OUTPUT_PRICE_USD
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "cost_czk": cost_usd * USD_TO_CZK,
    }


def main():
    articles = fetch_articles()

    # Ohodnotit poutavost a vybrat (≥3)
    selected = []
    for a in articles:
        rating = llm_rank_article(a["title"], a["summary"])
        a["rating"] = rating
        if rating >= 3:
            selected.append(a)

    # Vygenerovat obsah pro vybrané
    os.makedirs("posts", exist_ok=True)  # vytvoří složku posts, pokud neexistuje
    for a in selected:
        a["article_md"] = llm_generate_article(a["title"], a["summary"], a["link"])
        a["linkedin_posts"] = llm_generate_linkedin_posts(a["title"], a["summary"])
        slug = slugify(a["title"])[:60]
        a["file_name"] = f"posts/post_{slug}.html"


    # Časový rozsah na index
    if selected:
        start_date = min(x["published"] for x in selected).strftime("%d.%m.%Y")
        end_date = max(x["published"] for x in selected).strftime("%d.%m.%Y")
    else:
        today = datetime.now().strftime("%d.%m.%Y")
        start_date = end_date = today

    # Odhad nákladů (model) + reálné usage z API
    usage_est = estimate_costs(n_items=len(articles), n_selected=len(selected))
    total_tokens = total_prompt_tokens + total_completion_tokens
    usage_real = {
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
    }

    # Zápis indexu
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(render_index_html(selected, start_date, end_date, usage_real, usage_est))

    # Zápis detailů
    for a in selected:
        with open(a["file_name"], "w", encoding="utf-8") as f:
            f.write(render_post_html(a))

    # Log
    print(
        f"[ODHAD] input≈{int(usage_est['input_tokens'])}, output≈{int(usage_est['output_tokens'])}, "
        f"USD≈${usage_est['cost_usd']:.4f} / CZK≈{usage_est['cost_czk']:.2f}"
    )
    print(
        f"[MERENO] input={total_prompt_tokens}, output={total_completion_tokens}, total={total_tokens}"
    )


if __name__ == "__main__":
    main()
