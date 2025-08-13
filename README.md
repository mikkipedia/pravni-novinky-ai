## 💸 Odhad nákladů na 1 spuštění (tokeny → $)

**Jak se počítá cena:**  
OpenAI účtuje zvlášť **vstupní tokeny** a **výstupní tokeny**. Pro jednoduchost držíme v proměnných níže **jednotkové ceny** a **průměrné tokeny na položku**. Pokud se ceník někdy změní, přepiš si hodnoty a spočti znovu.

**Výchozí předpoklady (lze upravit):**
- `input_price = 0.15 / 1_000_000` USD/token (vstup)  
- `output_price = 0.60 / 1_000_000` USD/token (výstup)  
- průměr na **1 RSS položku** (klasifikace poutavosti): `in_cls = 300`, `out_cls = 1`  
- průměr na **1 vybraný článek (rating 3–5)**:  
  - Blog generování: `in_blog = 350`, `out_blog = 700`  
  - 3× LinkedIn posty dohromady: `in_li = 300`, `out_li = 220`

**Vzorec (pro 1 běh):**
- měj `N = počet načtených položek z RSS`  
- `p = podíl vybraných (0–1)` → tedy `N_sel = N * p`

Celkové tokeny:
- `input_tokens = N*in_cls + N_sel*(in_blog + in_li)`  
- `output_tokens = N*out_cls + N_sel*(out_blog + out_li)`

Cena:
- `cost_usd = input_tokens*input_price + output_tokens*output_price`

### Rychlé příklady
| N (položky) | p (vybrané) | input tok. | output tok. | cena/run |
|---:|---:|---:|---:|---:|
| 30 | 0.40 | ~16 800 | ~11 070 | ≈ **$0.009** |
| 60 | 0.40 | ~33 600 | ~22 140 | ≈ **$0.018** |
| 90 | 0.50 | ~56 250 | ~41 490 | ≈ **$0.033** |
| 150 | 0.50 | ~93 750 | ~69 150 | ≈ **$0.056** |

> Tip: Pokud chceš šetřit, používej kratší prompty nebo sniž vybrané (`p`) filtrem (např. přísnější klíčová slova před LLM).

### Mini kalkulačka (lokálně / v hlavě repa)
V Pythonu si můžeš rychle přepočítat cenu (změň si N a p podle reality):

```python
# RYCHLÝ ODHAD – uprav si N a p:
N = 60         # počet RSS položek za posledních 30 dní
p = 0.4        # podíl vybraných (3–5)

# jednotkové ceny (USD/token)
input_price = 0.15 / 1_000_000
output_price = 0.60 / 1_000_000

# průměrné tokeny
in_cls, out_cls = 300, 1
in_blog, out_blog = 350, 700
in_li, out_li = 300, 220

N_sel = N * p
input_tokens = N*in_cls + N_sel*(in_blog + in_li)
output_tokens = N*out_cls + N_sel*(out_blog + out_li)
cost_usd = input_tokens*input_price + output_tokens*output_price

print(f"Input tokens:  {int(input_tokens):,}")
print(f"Output tokens: {int(output_tokens):,}")
print(f"Odhad ceny:    ${cost_usd:.3f} / run")
