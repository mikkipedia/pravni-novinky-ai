import os
import re
import requests
import feedparser
from datetime import datetime, timedelta
from urllib.parse import urlparse
from openai import OpenAI
from html import escape as escape_html

# ====== CONFIG ======
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

# Počet dní zpět pro sběr článků (můžeš si změnit později na 1)
DAYS_BACK = 30

# RSS zdroje
RSS_FEEDS = [
    "https://www.epravo.cz/rss.php",
    "https://www.pravniprostor.cz/rss/zpravy",
    "https://www.bulletin-advokacie.cz/rss",
]

# ====== USAGE TRACKING ======
total_prompt_tokens = 0
total_completion_tokens = 0

def add_usage(resp):
    global total_prompt_tokens, total_completion_tokens
    usage = resp.usage
    total_prompt_tokens += usage.prompt_tokens
    total_completion_tokens += usage.completion_tokens

# ====== FETCH ======
def fetch_articles():
    articles = []
    cutoff = datetime.now() - timedelta(days=DAYS_BACK)
    for feed_url in RSS_FEEDS:
        parsed = feedparser.parse(feed_url)
        for entry in parsed.entries:
            try:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6])
                else:
                    published = datetime.now()
                if published < cutoff:
                    continue
                link = entry.link
                title = entry.title
                summary = getattr(entry, "summary", "")
                category = getattr(entry, "category", "")
                articles.append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "published": published,
                    "source": urlparse(feed_url).netloc,
                    "category": category
                })
            except Exception as e:
                print(f"Chyba při parsování článku: {e}")
    return articles

# ====== LLM ======
def llm_rank_article(title, summary):
    user = f"Title: {title}\nSummary: {summary}\n\nRate interest 1-5 (1=boring, 5=groundbreaking). Respond with just the number."
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": user}],
        max_tokens=5,
    )
    add_usage(resp)
    content = resp.choices[0].message.content.strip()
    try:
        return int(content)
    except:
        return 1

def llm_generate_article(title, summary, link):
    user = f"""
Napiš čtivý článek srozumitelný pro širokou veřejnost na základě níže uvedených informací. 
Použij patkové písmo pro text, odkaz na zdroj uveď na začátku nebo konci, 
a organicky vlož odkaz na https://www.springwalk.cz/pravni-poradenstvi/ tak, aby nepůsobil násilně.

Titulek: {title}
Anotace: {summary}
Zdroj: {link}
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": user}],
        temperature=0.7,
        max_tokens=1400,
    )
    add_usage(resp)
    text = resp.choices[0].message.content.strip()
    if "springwalk.cz/pravni-poradenstvi" not in text:
        text += f"\n\nVíce informací: [Spring Walk](https://www.springwalk.cz/pravni-poradenstvi/)"
    return text

def llm_generate_linkedin_posts(title, summary):
    user = f"""
Vytvoř 3 příspěvky na LinkedIn (každý 4–6 vět) k tématu níže.
Každý blok začni nadpisem:
"Společnost Spring Walk:"
"Jednatel (formální):"
"Jednatel (hravý):"
Bloky odděl třemi pomlčkami ---.
Nepoužívej odrážky.

Titulek: {title}
Anotace: {summary}
"""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": user}],
        temperature=0.7,
        max_tokens=680,
    )
    add_usage(resp)
    raw = (resp.choices[0].message.content or "").strip()
    blocks = [b.strip() for b in re.split(r'\n?---\n?', raw) if b.strip()]
    html_blocks = []
    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if lines:
            heading = lines[0].rstrip(":")
            body = " ".join(lines[1:])
            html_blocks.append(f'<div class="li-post"><div class="li-heading"><strong>{escape_html(heading)}</strong></div><div class="li-body">{escape_html(body)}</div></div>')
    while len(html_blocks) < 3:
        html_blocks.append('<div class="li-post"><div class="li-heading"><strong>Příspěvek</strong></div><div class="li-body"></div></div>')
    return html_blocks

# ====== HTML RENDER ======
def md_links_to_html(text):
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

def md_to_html(txt):
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

def render_index_html(articles, start_date, end_date):
    items = []
    for art in articles:
        badge_html = f'<div class="badge">{escape_html(art["category"])}</div>' if art.get("category") else ""
        items.append(f'''
<div class="card">
  {badge_html}
  <h2><a href="{art["file_name"]}">{escape_html(art["title"])}</a></h2>
  <div class="meta">{art["source"]} — {art["published"].strftime("%d.%m.%Y")}</div>
</div>
''')
    return f'''<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>Právní novinky</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>Právní novinky ({start_date} – {end_date})</h1>
  <div class="grid">
    {''.join(items)}
  </div>
</div>
</body>
</html>'''

def render_post_html(art):
    return f'''<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<title>{escape_html(art["title"])}</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<div class="wrap">
  <h1>{escape_html(art["title"])}</h1>
  <div class="meta">{art["source"]} — {art["published"].strftime("%d.%m.%Y")}</div>
  {f'<div class="badge">{escape_html(art["category"])}</div>' if art.get("category") else ""}
  <div class="article">
    {md_to_html(art["article_html"])}
  </div>
  <h2>Příspěvky na LinkedIn</h2>
  {''.join(art["linkedin_posts"])}
</div>
</body>
</html>'''

# ====== MAIN ======
def main():
    arts = fetch_articles()
    ranked = []
    for art in arts:
        score = llm_rank_article(art["title"], art["summary"])
        if score >= 3:
            ranked.append(art)
    for art in ranked:
        art["article_html"] = llm_generate_article(art["title"], art["summary"], art["link"])
        art["linkedin_posts"] = llm_generate_linkedin_posts(art["title"], art["summary"])
        art["file_name"] = f"post_{abs(hash(art['title']))}.html"
    if ranked:
        start_date = min(a["published"] for a in ranked).strftime("%d.%m.%Y")
        end_date = max(a["published"] for a in ranked).strftime("%d.%m.%Y")
    else:
        start_date = end_date = datetime.now().strftime("%d.%m.%Y")
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(render_index_html(ranked, start_date, end_date))
    for art in ranked:
        with open(art["file_name"], "w", encoding="utf-8") as f:
            f.write(render_post_html(art))
    usd_cost = (total_prompt_tokens/1_000*0.00015 + total_completion_tokens/1_000*0.0006)
    czk_cost = usd_cost * 24
    print(f"Použito tokenů: prompt={total_prompt_tokens}, completion={total_completion_tokens}")
    print(f"Odhadovaná cena: {usd_cost:.4f} USD (~{czk_cost:.2f} CZK)")

if __name__ == "__main__":
    main()
