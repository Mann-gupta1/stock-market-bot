# Regulatory and market-structure facts (India-resident individual, own account)

Verified: 2026-11-02. "Conf" means confidence: H = read in the primary source text; M = official broker doc or primary source read only in part; L = secondary source only (marked UNVERIFIED).
This is not legal or tax advice. Re-verify anything marked M or L before live trading.

## 1. SEBI / NSE retail algo framework

| Item | Finding | Source (published) | Conf |
|---|---|---|---|
| Base circular | SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013, "Safer participation of retail investors in Algorithmic trading". Original effective date was 2025-08-01. | https://www.sebi.gov.in/sebi_data/attachdocs/feb-2025/1738665456458.pdf (2025-02-04) | H |
| Effective dates as applied | Extended to 2025-10-01 by a circular dated 2025-07-29. Brokers that were ready went live on 2025-10-01. Glide path: apply for registration by 2025-10-31, register by 2025-11-30, complete a mock session by 2026-01-03. Brokers that miss these milestones are barred from onboarding new API-algo clients from 2026-01-05. **From 2026-04-01 the framework applies to all brokers.** | SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/132, https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf (2025-09-30) | H |
| OPS threshold | The Threshold Order Per Second (TOPS) is **10 orders/sec per exchange (per segment, para F)**. It is measured on the calendar clock second of the broker's server. The exchange may change it after notice. A broker may set a lower per-client limit, and it rejects orders above the limit. | NSE/INVG/67858, https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf (2025-05-05) | H |
| Self-built algo, own account | Registration with the exchange (through the broker) is needed **only above the threshold**. A registered algo may also be used by family: self, spouse, dependent children, dependent parents. Below 10 OPS the client does not register, but orders still carry an exchange-issued **generic algo ID** (INVG67858 para B.3, G). | SEBI Feb-2025 para I(c); INVG67858 B.2–B.3, C.1 | H |
| Static IP | Static IP is mandatory for API access for client-generated algos. A client may have one primary and one optional secondary IP. The IP can change at most once a calendar week. One IP maps to one client, but family may share one. Brokers must not run open APIs; access needs a client-specific API key plus a whitelisted IP. | INVG67858 para A.1–A.7, I(e) | H |
| Session / auth | API sessions must be logged out daily before the next trading day. Authentication is OAuth only, with 2FA. | INVG67858 A.8, I(c)–(d); SEBI Feb-2025 I(d) | H |
| Algo ID tagging | Every algo order, below and above the threshold, carries an exchange-provided unique identifier for the audit trail. | INVG67858 para G; SEBI Feb-2025 II(b) | H |
| Broker duties | The broker is principal and the algo vendor is its agent. The broker must tell algo orders from non-algo orders, enforce TOPS and RMS checks, keep an audit trail for 5 years, and handle grievances. It is fully liable for all API orders. Non-registered algos may run only on one predefined API key. The exchange can kill rogue algos. | SEBI Feb-2025 I–II; INVG67858 A.4, B, I, J | H |
| Later NSE operational modalities | SEBI says exchanges issued operational modalities on 2025-07-22, plus clarifications in the second half of September 2025. These were not read. | Cited in SEBI CIR/2025/132 para 2 | M |

## 2. NSE cash equity market structure

| Item | Finding | Source (published) | Conf |
|---|---|---|---|
| Tick size | <₹250: 0.01. ₹250–1,000: 0.05. >1,000–5,000: 0.10. >5,000–10,000: 0.50. >10,000–20,000: 1.00. >20,000: 5.00. ETFs are excluded. Effective 2025-04-15. **Reviewed monthly:** a new tick applies from the first trading day of the month, based on the last close of the previous month. Read the tick from the daily security file, never hardcode it. | NSE/CMTR/67133, https://nsearchives.nseindia.com/content/circulars/CMTR67133.pdf (2025-03-13) | H |
| Session timings | Block deal 1: 08:45–09:00. Pre-open: 09:00–09:15. Continuous (normal) market: 09:15–15:30. T+0 market: 09:15–13:30. Block deal 2: 14:05–14:20. Closing session: 15:40–16:00. Trade-modification cut-off: 16:15. | https://www.nseindia.com/static/market-data/market-timings (fetched 2026-11-02) | H |
| Pre-open (current) | Pre-open runs 09:00–09:15. Order entry: limit and market orders 09:00–09:05, limit orders only 09:05–09:10, with a random close in the last 2 min. Matching: 09:10–09:12. Buffer: 09:12–09:15. SL, IOC and DQ orders are not allowed. *Conflict:* the market-timings page still shows "pre-open close 09:08". Treat the pre-open product page as current. A secondary source dates this change to 2026-09-07 (UNVERIFIED). | https://www.nseindia.com/static/products-services/equity-market-pre-open | M |
| Closing Auction Session | Applies to cash stocks that have derivatives (phase 1). Session runs 15:15–15:35. Reference price is the VWAP of 15:00–15:15. Order entry runs 15:20–15:30 (limit orders only from 15:25, random close). Post-close: 15:50–16:00. Band: ±3%. Non-CAS stocks trade continuously until 15:30. Go-live was 2026-08-03. | https://www.nseindia.com/static/products-services/closing-auction-session ; NSE/FAOP/75472 https://nsearchives.nseindia.com/content/circulars/FAOP75472.pdf (2026-07-30) | H |
| Block deals | Morning window: 08:45–09:00, reference price = previous close. Afternoon window: 14:05–14:20, reference price = VWAP of 14:00–14:05. Price range: ±3%. **Minimum ₹25 cr.** Trades must result in delivery. Effective about 60 days after the circular (secondary sources say 2025-12-07). | SEBI/HO/MRD/POD-III/CIR/P/2025/134, https://www.sebi.gov.in/sebi_data/attachdocs/oct-2025/1759928647818.pdf (2025-10-08) | H |
| 2026 holidays | 15 weekday CM holidays (NSE/CMTR/71775), plus 2026-01-15 added by NSE/CMTR/72260. Muhurat session: Sunday 2026-11-08. **Remaining in 2026: Nov 10, Nov 24, Dec 25.** | https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf (2025-12-12); https://nsearchives.nseindia.com/content/circulars/CMTR72260.pdf (2026-01-12) | H |
| Settlement | T+1 is standard. Optional T+0 coexists with it, expanding in phases from 25 scrips to the top 500. Any broker may offer T+0, but the deadline for Qualified Stock Brokers to be T+0-ready was extended without a date (2025-10-30). | SEBI/HO/MRD/MRD-PoD-3/P/CIR/2024/172 (2024-12-10) https://www.sebi.gov.in/legal/circulars/dec-2024/enhancement-in-the-scope-of-optional-t-0-rolling-settlement-cycle-in-addition-to-the-existing-t-1-settlement-cycle-in-equity-cash-markets_89443.html ; https://www.sebi.gov.in/sebi_data/attachdocs/oct-2025/1761825190101.pdf | H (rules) / M (current T+0 scrip count unknown) |

## 3. Indian equity transaction costs (NSE)

| Charge | Intraday (MIS) | Delivery (CNC) | Source | Conf |
|---|---|---|---|---|
| STT | 0.025% on the sell side | 0.1% on buy and sell | Zerodha charges page, https://zerodha.com/charges/ (fetched 2026-11-02). Cash-equity STT did not change in Budget 2024 or 2026. Budget 2026 raised F&O STT only, from 2026-04-01: futures 0.05%, options 0.15% (from broker doc and several secondary sources). | M (statute not read) |
| NSE transaction charge | 0.00307% of turnover, both sides | same | Zerodha charges page. NSE moved to a flat ₹2.97/lakh from 2024-10-01 (secondary sources). The gap to 3.07 is likely the IPFT levy (UNVERIFIED). | M |
| SEBI turnover fee | ₹10/crore (0.0001%) | same | Zerodha charges page | M |
| Stamp duty (buy side only) | 0.003% (₹300/cr) | 0.015% (₹1,500/cr) | Zerodha charges page. Uniform rate since 2020-07-01 under the Indian Stamp Act amendment; the gazette was not read. | M |
| GST | 18% on (brokerage + exchange transaction charge + SEBI fee) | same | Zerodha charges page | M |

## 4. NSE market data

| Item | Finding | Source | Conf |
|---|---|---|---|
| Level 1 | Best bid/ask plus LTP, volume and OHLC. Brokers expose this as "quote", "ltpc" or "ltp" modes. | Broker docs (§5) | M |
| Level 2 / 5-depth | Top 5 bid and ask price levels. This is the standard "full" mode on Kite and Upstox. Dhan adds 20-depth and 200-depth; Upstox Plus adds 30-depth. | Broker docs (§5) | H |
| Tick-by-tick | TBT is a separate NSE Data & Analytics product that carries the full order book. It is delivered by multicast over a leased line, or through authorised vendors, and is licensed and paid. Retail broker WebSockets are **snapshots, not TBT**: Zerodha staff describe about 1 snapshot/sec per instrument. | https://www.nseindia.com/static/market-data/real-time-data-subscription ; Kite forum https://kite.trade/forum/discussion/1291/frequency-of-ticks | M |
| Redistribution | Trading members and subscribers may **not redistribute** market data unless their agreement allows it. They may not use it to create indices. Non-display or derived-data use falls under a separate NSE policy and fee. Personal, non-redistributed use through a broker API is the safe envelope; never publish raw feed data or derived data. | NSE Data Usage & Sharing Policy https://nsearchives.nseindia.com/web/sites/default/files/inline-files/NSE_DataUsageandSharingPolicy.pdf ; Non-display policy https://nsearchives.nseindia.com/web/sites/default/files/inline-files/Non_Display_Policy.pdf | M (from search extract; PDFs not read in full) |

## 5. Broker APIs (official docs, fetched 2026-11-02)

| | Zerodha Kite Connect | Upstox | Dhan HQ v2 | Angel One SmartAPI |
|---|---|---|---|---|
| WS depth | Modes `ltp`, `quote`, `full`; full = 5-depth. Limits: 3,000 instruments per connection, 3 connections per key. | Modes `ltpc`, `option_greeks`, `full` (5-depth), `full_d30` (30-depth, Upstox Plus only). Standard plan: 2 connections. | 5-depth feed; 20-depth (Level 3) and 200-depth (1 instrument per connection) need the Data API plan. | **UNVERIFIED.** Secondary sources mention a 20-depth WS mode. |
| Order rate limit | 10 req/s, 400 orders/min, 5,000 orders/day per user. 25 modifications per order. Quote endpoint: 1 req/s. | Unregistered algo: 10 req/s, 500/min, 2,000/30 min. Registered algo: 50/s. | Orders: 10/s, 250/min, 1,000/hr, 7,000/day. 25 modifications per order. Data: 5/s. Quote: 1/s. | **UNVERIFIED.** Secondary sources say 9 orders/s, 500/min, 1,000/hr. |
| Sandbox / paper | **None**. The FAQ says "Zerodha does not offer a sandbox". | Yes: sandbox for place/modify/cancel orders only, token valid 30 days, no market data documented. | Yes: a sandbox developer kit exists, scope not documented. | UNVERIFIED |
| Static IP | Required for **order placement** only; data and other endpoints work from any IP. Family may share one IP. | Required for place/modify/cancel/GTT/exit-all (error UDAPI1154). One primary plus one secondary IP, changeable weekly. Only one active app per user from 2026-04-01 (secondary source). | Required for place/modify/cancel, including super and forever orders. Primary plus secondary IP; locked for 7 days after a change. Error DH-905. | Required for orders and GTT (forum post by Angel One). |
| Client tag / idempotency | `tag`, max 20 alphanumeric chars. **No idempotency key.** | `tag`, max 40 chars. Optional `X-Algo-Name` header. `slice` and `market_protection` params. No idempotency. | `correlationId`, max 30 chars. Orders can be looked up by correlationId. No dedupe documented. | UNVERIFIED |
| Cost | Personal plan: free, orders only, no market data. Connect: ₹500/month per app, adds WS and historical data. | Trading and data APIs are free; brokerage applies. | Trading APIs free. Data API: ₹499 + GST per month. | Free (secondary) |
| Docs | https://kite.trade/docs/connect/v3/ , https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/kite-connect-api-faqs | https://upstox.com/developer/api-documentation/ (rate-limiting, v3/get-market-data-feed, sandbox, v3/place-order) | https://dhanhq.co/docs/v2/ , /orders/ ; https://dhan.co/support/platforms/dhanhq-api/ | https://smartapi.angelone.in/ (not read) |
| Conf | H | H | H (rates, IP, tag) / M (sandbox, pricing) | L |

Design implication: with no broker idempotency key, the platform must make order submission idempotent itself. Generate a unique tag per intent, then on timeout or retry reconcile against the order book by tag before resubmitting.

## 6. Overseas: LRS, TCS, IBKR

| Item | Finding | Source (published) | Conf |
|---|---|---|---|
| LRS limit | USD 250,000 per financial year (Apr–Mar) per resident individual, current and capital account combined. PAN is mandatory. Unused foreign exchange must be repatriated within 180 days unless reinvested. | RBI FAQ https://www.rbi.org.in/commonperson/English/Scripts/FAQs.aspx?Id=1834 (updated 2023-04-06); Master Direction https://www.rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=10192 | H |
| Margin prohibition | LRS does **not** cover "Remittance from India for margins or margin calls to overseas exchanges / overseas counterparty" or "Remittance for trading in foreign exchange abroad". | RBI FAQ Q2 (above) | H |
| IBKR for residents | The overseas-only account offers 90+ markets, excluding NSE. It holds stocks, bonds and ETFs and is a **cash account only**. "On non-Indian markets, Indian residents are prohibited from trading futures, options or margin-based products." Age 18+. USD 500 minimum to keep market-data subscriptions. A separate IB India margin account is available for NSE. | https://www.interactivebrokers.co.in/en/accounts/individual-account-india.php (fetched 2026-11-02) | H |
| IBKR per-exchange access (US, JP, UK, HK, AU, DE, FR, CA) | **UNVERIFIED.** IBKR states "90+ worldwide markets" but no per-country list for India-resident accounts was found. Confirm in the IBKR market-permissions screen before coding. | — | L |
| TCS on LRS | Investment remittances: 20% TCS on the amount above ₹10 lakh per FY, per PAN, from 2025-04-01. The TCS is creditable against income tax. From 2026-04-01 the provision reportedly moved to s.394 of the Income-tax Act 2025. **UNVERIFIED** in primary text (Finance Act not read). | Standard Chartered bank notice https://www.sc.com/in/important-information/lrs-amendment-in-tcs ; secondary tax blogs | L/M |

## 7. US day-trading rules

| Item | Finding | Source (published) | Conf |
|---|---|---|---|
| PDT rule status | **The pattern-day-trader rule is removed.** FINRA amended Rule 4210 to delete the day-trade-count test and the $25k minimum equity, replacing them with an intraday margin standard for margin accounts. Effective 2026-06-04; firms may phase in until 2027-10-20. SEC approval release no. 34-105226 (2026-04-14, secondary). | FINRA Regulatory Notice 26-10 https://www.finra.org/rules-guidance/notices/26-10 (2026-04-20) | H |
| Cash accounts | The PDT rule never applied to cash accounts, and the new rule is a margin-account standard. Cash-account limits come from **Reg T 12 CFR 220.8**. **Free-riding** (buying and selling before paying) and **good-faith violations** (selling a security bought with unsettled proceeds before those proceeds settle) can bring a **90-day freeze**, during which purchases must be fully paid on trade date. US equity settlement is T+1. | FINRA https://www.finra.org/investors/investing/investment-accounts/brokerage-accounts/cash-accounts ; FINRA https://www.finra.org/investors/insights/frequent-intraday-trading ; SEC/Investor.gov https://www.sec.gov/files/investor/alerts/cashaccounts.pdf | H |
| Implication | Because LRS money must sit in an IBKR cash account, intraday round trips are limited by **settled cash**. The PDT repeal does not help here. | Derived | H |

## 8. Foreign transaction taxes on intraday round trips

| Market | Finding | Source (published) | Conf |
|---|---|---|---|
| UK | Electronic purchases of UK shares pay SDRT of **0.5%, paid by the buyer, on every purchase**. There is no intraday netting for a retail account, so a round trip costs 0.5% of the buy leg. Intermediary relief is for recognised intermediaries (exchange members), not retail. Shares of foreign companies bought outside the UK are generally out of scope. Growth-market (e.g. AIM) shares are exempt. | https://www.gov.uk/tax-buy-shares ; https://www.gov.uk/guidance/stamp-duty-reserve-tax-reliefs-and-exemptions | H (rate) / M (no-netting reading) |
| France | The FTT is **0.4% from 2025-04-01** (Law 2025-127). The base is the **net buying position at end of day** for intraday trades with the same settlement date, so a fully flat intraday round trip leaves no base (inference from §130; BOFiP does not say so explicitly). Scope covers shares of French companies with market cap above €1bn (list published yearly). The scope rule was not re-read: UNVERIFIED. | BOFiP BOI-TCA-FIN-10-30 https://bofip.impots.gouv.fr/bofip/7575-PGP.html/identifiant=BOI-TCA-FIN-10-30-20250528 (2025-05-28) | H (rate) / M (intraday) |
| Hong Kong | Stamp duty is **0.1% on the buyer and 0.1% on the seller** of each trade, rounded up to the next HK$1. In force since 2023-11-17 (cut from 0.13%). An intraday round trip costs about 0.2%. | https://www.gov.hk/en/residents/taxes/stamp/stamp_duty_rates.htm ; https://www.info.gov.hk/gia/general/202311/15/P2023111500359.htm (2023-11-15) | H |

## Assumptions the platform must keep configurable

- `ops_limit_per_exchange` (default 10) and a per-broker cap below it. The exchange can revise it, and the broker may set it lower. Throttle by broker-server clock second.
- `static_ip_primary` / `static_ip_secondary`. A change is allowed at most once a week. Treat IP mismatch errors (UDAPI1154, DH-905) as hard stops, not retries.
- `algo_id`: the exchange-assigned generic or registered algo ID, if a broker requires the client to pass it.
- Session times per market: pre-open, continuous, CAS, closing session, block windows. Read the holiday calendar from a file loaded from NSE each year, including ad-hoc holidays (e.g. 2026-01-15).
- Tick size per symbol, refreshed monthly from the NSE security file. Never derive it from the price-band table alone.
- Settlement cycle per market (IN T+1 or optional T+0; US T+1) and a settled-cash ledger for cash accounts.
- Cost model per market and product: STT, exchange transaction charge, SEBI fee, stamp duty, GST, brokerage, UK SDRT, FR FTT (with the flag for intraday netting), HK stamp duty with rounding. All rates are versioned with effective dates.
- Broker rate limits: orders per second, minute and day; modifications per order; quote and historical request limits.
- Market-data depth mode per broker (5, 20, 30 or 200 levels) and the snapshot interval assumption (about 1/s, not TBT).
- Tag/correlation ID length per broker (Kite 20, Dhan 30, Upstox 40), plus app-side idempotency.
- `market_protection` percentage for market and SL-M orders. Exchanges restrict unprotected API market orders.
- LRS annual cap (USD 250k), TCS threshold and rate (₹10 lakh, 20%), and a hard `overseas_margin_allowed=false`.
- Permitted overseas exchanges and products, from IBKR account permissions (stocks, ETFs and bonds only; no derivatives or margin).
