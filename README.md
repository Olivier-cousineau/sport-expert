# sport-expert

Scraper Playwright (Python) pour extraire les produits de liquidation Sports Experts (50%+).

## Installation

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Exécution

```bash
python scripts/scrape_sportsexperts_clearance_50.py
```

Options utiles :

```bash
python scripts/scrape_sportsexperts_clearance_50.py --max-cycles 40 --stable-cycles 6
python scripts/scrape_sportsexperts_clearance_50.py --debug
DEBUG=1 python scripts/scrape_sportsexperts_clearance_50.py
```

## Sorties

- `outputs/sportsexperts_liquidation_50plus.csv`
- `outputs/sportsexperts_liquidation_50plus.json`

En mode debug, le script sauvegarde aussi :

- `outputs/debug/se_after_load.html`
- `outputs/debug/se_after_load.png`
