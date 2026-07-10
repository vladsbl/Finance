# Couverture internationale - yfinance / Finnhub / Yahoo RSS

_Rapport genere le 2026-07-10 10:23 par `diagnostics/test_international_coverage.py`._

Diagnostic isole : ces tickers ne sont PAS integres au pipeline. Objectif = voir ou chaque source fonctionne, est degradee ou vide, pour savoir ou etendre l'univers suivi de facon realiste.

## Tableau recapitulatif

| Ticker | Pays | yfinance prix | yfinance info | Finnhub news | Finnhub profil | RSS Yahoo |
|--------|------|---------------|---------------|--------------|----------------|-----------|
| `LVMH.PA` | France (Paris) | aucune donnee | nom seul | HTTP 403 | HTTP 403 | 0 items |
| `SAP.DE` | Allemagne (Francfort) | 23 barres, dern. 2026-07-10 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 20 items |
| `ASML.AS` | Pays-Bas (Amsterdam) | 23 barres, dern. 2026-07-10 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 20 items |
| `HSBA.L` | Royaume-Uni (Londres) | 23 barres, dern. 2026-07-10 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 20 items |
| `7203.T` | Japon (Tokyo) | 23 barres, dern. 2026-07-10 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 20 items |
| `600519.SS` | Chine (Shanghai A-share) | 22 barres, dern. 2026-07-10 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 8 items |
| `0700.HK` | Hong Kong | 21 barres, dern. 2026-07-10 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 20 items |
| `005930.KS` | Coree du Sud (Seoul) | 23 barres, dern. 2026-07-10 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 20 items |
| `RELIANCE.NS` | Inde (NSE) | 23 barres, dern. 2026-07-10 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 0 items |
| `PETR4.SA` | Bresil (Sao Paulo) | 22 barres, dern. 2026-07-08 | OK (nom+secteur+description) | HTTP 403 | HTTP 403 | 20 items |

## Conclusion par zone

### Europe
- **LVMH.PA** (France (Paris)) : degrade/vide: prix yfinance KO, info yfinance partielle, Finnhub news vide, Finnhub profil vide, RSS vide
- **SAP.DE** (Allemagne (Francfort)) : OK: prix yfinance, info yfinance complete, RSS (20) | degrade/vide: Finnhub news vide, Finnhub profil vide
- **ASML.AS** (Pays-Bas (Amsterdam)) : OK: prix yfinance, info yfinance complete, RSS (20) | degrade/vide: Finnhub news vide, Finnhub profil vide
- **HSBA.L** (Royaume-Uni (Londres)) : OK: prix yfinance, info yfinance complete, RSS (20) | degrade/vide: Finnhub news vide, Finnhub profil vide

### Japon
- **7203.T** (Japon (Tokyo)) : OK: prix yfinance, info yfinance complete, RSS (20) | degrade/vide: Finnhub news vide, Finnhub profil vide

### Chine continentale
- **600519.SS** (Chine (Shanghai A-share)) : OK: prix yfinance, info yfinance complete, RSS (8) | degrade/vide: Finnhub news vide, Finnhub profil vide

### Hong Kong
- **0700.HK** (Hong Kong) : OK: prix yfinance, info yfinance complete, RSS (20) | degrade/vide: Finnhub news vide, Finnhub profil vide

### Coree du Sud
- **005930.KS** (Coree du Sud (Seoul)) : OK: prix yfinance, info yfinance complete, RSS (20) | degrade/vide: Finnhub news vide, Finnhub profil vide

### Inde
- **RELIANCE.NS** (Inde (NSE)) : OK: prix yfinance, info yfinance complete | degrade/vide: Finnhub news vide, Finnhub profil vide, RSS vide

### Bresil
- **PETR4.SA** (Bresil (Sao Paulo)) : OK: prix yfinance, info yfinance complete, RSS (20) | degrade/vide: Finnhub news vide, Finnhub profil vide

## Lecture d'ensemble

- **yfinance prix** : la source la plus robuste a l'international (suffixes .PA/.DE/.L/.T/.HK/.SS/.KS/.NS/.SA reconnus).
- **yfinance info** : qualite variable selon la place (secteur parfois vide hors US).
- **Finnhub** : company-news et profile2 sont pensees pour les tickers US ; la couverture hors US est souvent partielle ou vide sur le tier gratuit.
- **RSS Yahoo** : meme format d'URL, mais le volume chute fortement pour les tickers non-US (0 pour l'Inde).

### Attention aux symboles

- `LVMH.PA` n'est PAS un symbole Yahoo valide : le ticker Euronext Paris de LVMH est `MC.PA`. Sa ligne toute vide reflete un mauvais symbole, pas une absence de couverture.
- Finnhub renvoie **HTTP 403** (interdit) sur company-news et profile2 pour tous ces tickers non-US : ces endpoints ne sont pas couverts par le tier gratuit hors marche US (ils fonctionnent pour AAPL/MSFT/... dans le pipeline).

> Detail par ticker dans le tableau ci-dessus (valeurs mesurees).
