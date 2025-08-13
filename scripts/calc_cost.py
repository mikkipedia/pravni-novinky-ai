
---

# 🧮 Volitelný soubor `scripts/calc_cost.py` (praktické počítadlo)

Pokud chceš počítat z CLI: `python scripts/calc_cost.py --n 60 --p 0.4`

```python
# scripts/calc_cost.py
import argparse

def main():
    ap = argparse.ArgumentParser(description="Odhad nákladů na 1 run (tokeny → USD)")
    ap.add_argument("--n", type=int, required=True, help="počet RSS položek (N)")
    ap.add_argument("--p", type=float, required=True, help="podíl vybraných 0–1 (p)")
    ap.add_argument("--input_price", type=float, default=0.15/1_000_000, help="USD/token vstup (default 0.15/M)")
    ap.add_argument("--output_price", type=float, default=0.60/1_000_000, help="USD/token výstup (default 0.60/M)")
    # průměry tokenů:
    ap.add_argument("--in_cls", type=int, default=300)
    ap.add_argument("--out_cls", type=int, default=1)
    ap.add_argument("--in_blog", type=int, default=350)
    ap.add_argument("--out_blog", type=int, default=700)
    ap.add_argument("--in_li", type=int, default=300)
    ap.add_argument("--out_li", type=int, default=220)
    args = ap.parse_args()

    N_sel = args.n * args.p
    input_tokens = args.n*args.in_cls + N_sel*(args.in_blog + args.in_li)
    output_tokens = args.n*args.out_cls + N_sel*(args.out_blog + args.out_li)
    cost_usd = input_tokens*args.input_price + output_tokens*args.output_price

    print(f"Input tokens:  {int(input_tokens):,}")
    print(f"Output tokens: {int(output_tokens):,}")
    print(f"Odhad ceny:    ${cost_usd:.3f} / run")

if __name__ == "__main__":
    main()
