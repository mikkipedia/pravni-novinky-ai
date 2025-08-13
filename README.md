# Právní novinky – AI generátor

Automaticky sbírá právní novinky z českých RSS (epravo.cz, Advokátní deník, Právní prostor), ohodnotí jejich poutavost (1–5) pomocí LLM a pro témata se skóre 3–5 vygeneruje:
- článek pro blog (3–5 odstavců),
- 3 tipy na příspěvky pro LinkedIn.

Výstup je statický web (`index.html` + `/posts/*.html`), publikovaný přes GitHub Pages.

## Rychlý start

1. Vytvoř repo (např. `pravni-novinky-ai`) a vlož soubory z tohoto projektu.
2. V repozitáři otevři **Settings → Secrets and variables → Actions** a přidej **Repository secret**:
   - `OPENAI_API_KEY` = tvůj klíč z https://platform.openai.com/account/api-keys
3. (Volitelně) Uprav proměnné ve workflow `.github/workflows/generate.yml`:
   - `MODEL_NAME` (default `gpt-4o-mini`)
   - `DAYS_BACK` (default `30`)
4. Zapni **GitHub Pages**: Settings → Pages → Build and deployment → **Deploy from a branch** → Branch: `main`, Folder: `/`.
5. V Actions spusť workflow **Run workflow** (nebo počkej na plánovač v 06:00 UTC).
6. Web bude dostupný na `https://<username>.github.io/<nazev-repozitare>/`.

## Poznámky
- Styl je věcný, bez reklamy, v češtině.
- Zdrojové texty bere skript z RSS (titulek + perex). Pro prototyp to stačí.
- Tokenové náklady jsou nízké (doporučeno `gpt-4o-mini`).
- Design (CSS) lze doplnit později.
