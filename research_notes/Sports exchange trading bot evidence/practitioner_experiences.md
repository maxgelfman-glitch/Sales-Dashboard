# Practitioner Experiences: Automated Trading Bots on Sports Prediction Markets & Betting Exchanges (2023-2026)

> **Method caveat (read first):** In this session the network egress proxy blocked direct fetches of reddit.com (403 on CONNECT), dev.to, Medium, DL News, Finance Magnates, Prospect, Turbine, BotBlog and the Bet Angel forum. Every finding below therefore comes from **search-engine result snippets/summaries**, not full-page reads. Numbers are quoted as the snippets report them. I could not read individual Reddit threads (r/algobetting, r/Kalshi, r/Polymarket, etc.), so first-hand Reddit P&L posts are a **major gap**. Labels: **[DATA]** = on-chain/academic/official data; **[ANECDOTE]** = single first-hand account; **[PROMO]** = vendor/marketing/clickbait; **[NEWS]** = reported journalism; **[OFFICIAL]** = venue/regulator document.

## 1. What practitioners report about their bots: strategies, results, lifespans, why they stopped

### Takeaway
Documented first-hand bot stories cluster into (a) latency/stale-price exploits that worked briefly and were then killed by venue rule changes (Polymarket 15-min crypto, Feb 2026), (b) cross-venue Kalshi/Polymarket arbitrage with small 1.5-5% spreads and real leg/capital-velocity problems, and (c) in-game sports bots using free data feeds that failed because pros have faster data. Credible, verifiable, long-running *pregame US sports* bot P&L from solo developers is essentially absent from what I could reach.

### Cited Findings
- **[ANECDOTE, hobbyist/educational]** "Part Time Larry" (HackingTheMarkets) built a real-time Kalshi NFL bot that watched ESPN's unofficial win-probability API during the NFL playoffs and tried to buy before Kalshi repriced. It **did not work**: Kalshi traders and professional bettors have data feeds "at least 30 seconds faster" than the unofficial ESPN API. — [HackingTheMarkets](https://hackingthemarkets.com/anatomy-of-a-kalshi-nfl-trading-bot/); [YouTube "I built a Kalshi NFL Prediction Market Bot in Python (it was too slow)"](https://www.youtube.com/watch?v=bA_NUrMJuw4)
- **[ANECDOTE / possibly promotional]** A developer on DEV Community reported a "consistent 2-5% price spread" between Polymarket and Kalshi, capturing 1.5%-4.5% spreads on high-volume events (Fed rates, elections) by buying YES on one venue and NO on the other. Self-reported risks: prices move between API calls, you can get filled on one venue and stuck on the other, and fiat withdrawals from regulated exchanges "take days", slowing capital velocity. No audited P&L. — [DEV Community (realfishsam)](https://dev.to/realfishsam/how-i-built-a-risk-free-arbitrage-bot-for-polymarket-kalshi-4f)
- **[ANECDOTE]** A DEV Community post "February 2026 Changed Polymarket Forever - Here's What Happened to My Bot's Numbers": on **Feb 18, 2026** Polymarket quietly removed the 500ms taker delay and introduced dynamic taker fees; "the entire class of pure latency arbitrage strategies stopped working the same day"; "overnight, half of the bots on the platform became obsolete." Profit model shifted from taker arbitrage to maker liquidity + rebates. — [DEV Community (lkto1m)](https://dev.to/lkto1m/february-2026-changed-polymarket-forever-heres-what-happened-to-my-bots-numbers-2fi5); [Odaily "Polymarket's New Rules Released: How to Build a New Trading Bot"](https://www.odaily.news/en/post/5209447)
- **[NEWS, on-chain but sensationalized]** Wallet "0x8dxd" reportedly went from **$313 (Dec 2025) to ~$438,000 by Jan 6, 2026**, 98% win rate over 6,615 predictions, mostly BTC/ETH/SOL 15-minute up/down markets, exploiting lag between Binance/Coinbase spot and Polymarket prices. This was exactly the strategy class later killed by fees/delay changes. — [Bitget News / Kriptoworld](https://www.bitget.com/news/detail/12560605137629); [Shine Magazine](https://shine-magazine.com/trading-bot-polymarket-earnings/)
- **[NEWS/PROMO]** "Trading bot turns $63 into $131,000 on Polymarket in a month" — similar headline genre; treat as cherry-picked survivor. — [Finbold](https://finbold.com/trading-bot-turns-63-into-131k-on-polymarket-in-a-month/)
- **[ANECDOTE, backtest only]** Open-source repo `txrusso/kalshi-sports-trader` (recommend-only, MLB/NFL, money flow vs fair-value model) reports **+12.2% ROI on 308 held-out bets**, walk-forward backtested at real pregame Kalshi prices — a backtest, not live P&L, and a small sample. — [GitHub txrusso/kalshi-sports-trader](https://github.com/txrusso/kalshi-sports-trader)
- **[ANECDOTE, code only]** Other open-source Kalshi sports bots: XGBoost ensemble for NBA/NFL/MLB on Modal + Supabase ([GitHub Rohan5commit](https://github.com/Rohan5commit/kalshi-sports-bot)); a "delta" strategy vs public ESPN win probabilities ([GitHub lucavernhes-personal](https://github.com/lucavernhes-personal/kalshi-sports-bot)). No published live results found. A vendor blog lists "30+" open-source Kalshi bot projects. — [BotForKalshi (vendor)](https://www.botforkalshi.com/blog/open-source-kalshi-bot-ecosystem)
- **[FORUM, Betfair]** Some Betfair traders ran bots specifically to *churn commission* to manage their Premium Charge tier; when Betfair switched to a 52-week Expert Fee (Jan 2025) they turned those bots off. — [Bet Angel forum "Betfair have scrapped the Premium Charge"](https://forum.betangel.com/viewtopic.php?t=30079); [BotBlog Expert Fee explainer](https://botblog.co.uk/betfair-expert-fee-premium-charge/)

### Inferences
- The best-documented "big win" bot stories are in **crypto short-duration markets**, not pregame US sports, and they were short-lived windows closed by venue rule changes within weeks to months. They transfer poorly to a pregame sports trader.
- The failure pattern "free/public data feed vs. pros with paid low-latency feeds" is directly relevant: any in-game strategy on free data is likely adverse-selected. Pregame strategies depend on model quality or cross-venue price discrepancies, not speed.

### Gaps
- Could not read any Reddit threads (reddit.com blocked). No first-hand r/algobetting, r/Kalshi, r/Novig, r/SportsTrading P&L posts are captured here; these should be pulled by another means.
- No verifiable live P&L from a solo pregame US-sports bot on Kalshi, Novig, ProphetX or Sporttrade was found.
- No first-hand accounts found of bots on Novig, ProphetX or Sporttrade specifically (search returned only consumer comparison articles).

## 2. Betfair as the mature reference (older history labeled)

### Takeaway
Betfair shows the endgame of a mature exchange: the venue taxes consistent winners (Premium Charge 20% in 2008, up to 60% from 2011), which ended many "decent livings", while profitable participants were always a tiny fraction (<0.5% paid PC). In Jan 2025 Betfair replaced it with a rolling-52-week Expert Fee. Real-time API access also has a fixed cost.

### Cited Findings
- **[OFFICIAL/HISTORICAL, 2008]** Premium Charge introduced 2008 at 20% (applied to shortfall between commission paid and 20% of profits) for profitable customers active in >250 markets; <0.5% of customers paid it. — [Betfair Charges](https://www.betfair.com/aboutUs/Betfair.Charges/); [Wikipedia: Betting exchange](https://en.wikipedia.org/wiki/Betting_exchange)
- **[OFFICIAL/HISTORICAL, 2011]** Raised to up to 60% ("Super Premium Charge") in 2011, applying to <0.1% of annual active customers. — [Wikipedia: Betting exchange](https://en.wikipedia.org/wiki/Betting_exchange); [Caanberry PC explainer](https://caanberry.com/betfair-premium-charge-how-its-calculated/)
- **[FORUM, opinion]** Forum users: the 60% tier "certainly ended a lot of people's 'decent' livings, though there's still people making a decent living from the exchanges"; example: making £30k/yr for 8 years and then hitting 60% PC loses a large share of profits. — [Bet Angel forum "Betfair Premium Charge"](https://forum.betangel.com/viewtopic.php?t=24614&start=150); [GeeksToy forum](https://www.geekstoy.com/forum/forum/betting-trading/traders-exchange/5689-premium-charge/page2)
- **[OFFICIAL/VENDOR, 2025]** Expert Fee (from Jan 2025) applies only if lifetime account in profit, gross profit in last 52 active weeks > £25,000, and bet in >100 markets. — [BotBlog Expert Fee 2026](https://botblog.co.uk/betfair-expert-fee-premium-charge/)
- **[OFFICIAL]** Betfair API: free delayed key for development (1-180s delayed snapshots); Live App Key costs a one-off **£499** (previously £299), requires full KYC. — [Betfair Developer Program support](https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access); [Betfair dev forum "£299 for a live AppKey?"](https://forum.developer.betfair.com/forum/sports-exchange-api/exchange-api/3268-%C2%A3299-for-a-live-appkey)
- **[FORUM]** Long-running Bet Angel thread "can you really make money?" exists as a first-hand discussion source (not readable here). — [Bet Angel forum](https://forum.betangel.com/viewtopic.php?t=22028)

### Inferences
- Lesson for US exchanges: once a venue matures, the venue itself (fees, winner surcharges, fee redesigns, latency rules) is the biggest structural threat to small bot edges, alongside professional competition. Kalshi's tier/market-maker programs and Polymarket's Feb-2026 fee change are the same dynamic arriving faster.
- Betfair's <0.5%/<0.1% figures are consistent with the Polymarket on-chain concentration data below: long-run winners are a fraction of a percent.

### Gaps
- No quantified data on how many Betfair bot traders quit or how edges shrank year by year (only forum opinion). Syndicate competition on Betfair was not documented in reachable sources.

## 3. Polymarket/Kalshi bot economics: aggregate data and venue changes

### Takeaway
Aggregate data show large absolute bot profits (~$40M arbitrage on Polymarket in one year) but extreme concentration: ~84% of Polymarket wallets lose, ~2% ever made >$1,000, and only ~0.015% earned ≥$5k/month for four consecutive months. Kalshi's order book is dominated numerically by 2,000+ small makers/individuals, many running bots, alongside designated MMs like Susquehanna with fee/limit advantages.

### Cited Findings
- **[DATA, academic]** IMDEA Networks researchers (Saguillo, Ghafouri, Kiffer, Suarez-Tangil) analyzed 86M bets (Apr 2024-Apr 2025): ~**$40M** extracted by arbitrage — $10.58M single-condition, $23.28M market-rebalancing, $95,156 combinatorial. Top 3 wallets: >10,200 bets, **$4.2M** profit. — [DL News](https://www.dlnews.com/articles/markets/polymarket-users-lost-millions-of-dollars-to-bot-like-bettors-over-the-past-year/); [Yahoo Finance](https://finance.yahoo.com/news/arbitrage-bots-dominate-polymarket-millions-100000888.html)
- **[DATA, on-chain, Apr 2026]** Of ~2.5M Polymarket wallets, ~15.9% profitable, 84.1% lost money; only 2% ever made >$1,000; only **0.015%** made ≥$5,000 profit in each of four consecutive months (Apr 2024-Apr 2026). — [The Defiant](https://thedefiant.io/news/research-and-opinion/polymarket-profitability-report-april-2026)
- **[DATA, Apr 2026]** In politics markets (Dec 2025-Feb 2026), 0.55% of profitable maker wallets captured 50% of gains; 0.26% of winning taker wallets captured nearly as much. — [CoinDesk](https://www.coindesk.com/markets/2026/04/29/a-tiny-group-is-winning-on-polymarket-as-under-1-of-wallets-take-half-the-profits)
- **[DATA]** Earlier analysis: ~70% of Polymarket traders lost money; top 0.04% captured most (~70%) of realized profits. — [Yahoo Finance](https://finance.yahoo.com/news/70-polymarket-traders-lost-money-192327162.html)
- **[NEWS/OFFICIAL]** Polymarket dynamic taker fees on 5/15-minute crypto markets peak near 50¢ (reported ~1.56%, up to ~3.15% on a 50¢ contract in one report — sources differ), exceeding typical latency-arb margin; 100% of taker fees redistributed to makers. On Aug 17, 2026 taker delay on crypto markets cut from 250ms to 50ms. — [Finance Magnates](https://www.financemagnates.com/cryptocurrency/polymarket-introduces-dynamic-fees-to-curb-latency-arbitrage-in-short-term-crypto-markets/); [Unchained](https://unchainedcrypto.com/polymarket-introduces-taker-fees-in-15-minute-markets/); [KuCoin fee guide](https://www.kucoin.com/blog/polymarket-fees-trading-guide-2026). Note: the fee % and delay timelines conflict across sources (500ms removed Feb 18 per DEV post vs 250ms->50ms Aug 2026 per search summary); treat exact numbers as uncertain.
- **[NEWS/analysis]** Susquehanna built the first prediction-market desk (2023) and became Kalshi's first official designated market maker in early 2026 with reduced fees and higher position limits in exchange for quoting obligations; DRW hiring a desk (base up to $200k). Only ~5% of Kalshi bid matches come from major institutional MMs; ~95% from **2,000+ smaller market makers and individuals, many running bots**. — [Tradermath](https://www.tradermath.org/articles/prediction-markets-trading-at-quant-firms); [American Prospect](https://prospect.org/2026/08/26/house-always-wins-kalshi-prediction-markets/); [Turbine blog](https://www.turbinefi.com/blog/why-prediction-market-trades-get-picked-off-2026)
- **[NEWS]** Kalshi + Polymarket combined volume reached a record **$44B in June 2026**, vs < $100M/month in early 2024. — [Tradermath](https://www.tradermath.org/articles/prediction-markets-trading-at-quant-firms)
- **[OFFICIAL]** Kalshi taker fee = roundup(0.07 × C × P × (1−P)); max 1.75¢/contract at 50¢; maker fee = 0.0175 × C × P × (1−P) (25% of taker). Most sports series use multiplier 1: ~$3.50 taker fee on a $100 position at 50¢ (~1.75% of notional... reported as $3.50 for $100 position i.e. 200 contracts). — [Kalshi fee schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf); [TrueBet blog](https://blog.truebet.app/2026/08/kalshi-fees-explained-what-a-100-sports-trade-really-costs/); [Whirligig Bear substack](https://whirligigbear.substack.com/p/makertaker-math-on-kalshi)

### Inferences
- A solo developer with ~$100k competes with designated MMs who pay lower fees and get bigger limits; the "95% of fills from small makers" statistic means competition among bots at the top of book is crowded.
- At 50¢, Kalshi taker fees (~3.5% of cost for a 50¢ contract bought as taker: 1.75¢/50¢) consume most of a typical 2-5% cross-venue spread; arbitrage generally must be executed maker-side or at price extremes.

### Gaps
- No aggregate profitability study for **Kalshi** traders (Kalshi is not on-chain); no sports-specific breakdown of the Polymarket wallet studies.

## 4. Common pitfalls (operational, settlement, regulatory, tax)

### Takeaway
Reported pitfalls: venue outages during peak sports windows (with API users sometimes still trading and fills later refunded/reversed), leg risk and slow fiat withdrawals in cross-venue arb, oracle/resolution disputes on Polymarket, sudden unannounced rule/fee changes, a harsher 2026 tax regime for gambling-treated income, and live state-level legal action against Kalshi — including New York.

### Cited Findings
- **Outages / reversals [NEWS]:** Oct 2025 college-football Saturday: ~half of Kalshi users couldn't access app/website while the API kept working; API traders/automated MMs kept trading, and Kalshi **later refunded users whose resting orders were filled by API traders during the downtime**. Dec 2025 NFL Sunday (Bears-49ers): users couldn't close positions or view portfolios. Super Bowl point of failure also reported. — [CNBC Oct 18 2025](https://www.cnbc.com/2025/10/18/kalshi-down-for-some-users-during-college-football-games.html); [PiunikaWeb Dec 29 2025](https://piunikaweb.com/2025/12/29/kalshi-outage-blocks-users-from-closing-positions/); [DeFi Rate](https://defirate.com/news/kalshi-experiences-significant-point-of-failure-during-super-bowl/)
- **Leg risk / capital velocity [ANECDOTE]:** fills on one venue, stuck on the other; fiat withdrawals take days. — [DEV Community (realfishsam)](https://dev.to/realfishsam/how-i-built-a-risk-free-arbitrage-bot-for-polymarket-kalshi-4f)
- **Stale data [ANECDOTE]:** unofficial ESPN API ≥30s slower than pro feeds. — [HackingTheMarkets](https://hackingthemarkets.com/anatomy-of-a-kalshi-nfl-trading-bot/)
- **Settlement/resolution risk [NEWS]:** Polymarket UMA oracle: Mar 2025 "Ukraine mineral deal" market resolved YES despite no deal, after a large UMA holder cast ~5M tokens (~25% of votes); ~$7M paid out on a false resolution. Capital remains locked during proposal/challenge windows. (Note: Polymarket's US launch reportedly does not use UMA.) — [Orochi Network](https://orochi.network/blog/oracle-manipulation-in-polymarket-2025); [Sportico](https://www.sportico.com/business/sports-betting/2025/polymarket-uma-resolution-crypto-1234878760/); [Polymarket docs](https://docs.polymarket.com/concepts/resolution)
- **Unannounced rule changes [ANECDOTE/NEWS]:** Polymarket Feb 18, 2026 delay removal + dynamic fees with "no announcement". — [DEV Community (lkto1m)](https://dev.to/lkto1m/february-2026-changed-polymarket-forever-heres-what-happened-to-my-bots-numbers-2fi5)
- **Limits / integrity rules [NEWS/OFFICIAL]:** Kalshi has position accountability levels and can hard-cap markets; June 2026 integrity update requires employer disclosure for high-risk contracts and adds risk scoring. Kalshi API rate-limit tiers are volume-based. — [TheStreet](https://www.thestreet.com/investing/kalshi-imposes-stark-new-rule-for-certain-traders); [OddsShopper position limits](https://www.oddsshopper.com/articles/prediction-markets/kalshi-position-limits); [Kalshi API rate limits](https://docs.kalshi.com/getting_started/rate_limits)
- **Winner limits on US exchanges [marketing claims]:** Novig markets "no limits on winners"; ProphetX "no individual customer limits"; Sporttrade "does not limit winners". Novig and ProphetX received CFTC approval June 2026. — [OddsShopper ProphetX vs Novig](https://www.oddsshopper.com/articles/prediction-markets/prophetx-vs-novig); [CNBC Jun 16 2026](https://www.cnbc.com/2026/06/16/novig-wins-cftc-approval-as-competition-intensifies-in-sports-prediction-markets.html); [SI](https://www.si.com/prediction-markets/reviews/apps-like-novig)
- **Taxes [CPA/vendor guides]:** Kalshi does not issue a comprehensive 1099-B for all event-contract trades; reporting obligation exists regardless. Treatment options debated (Sec. 1256 60/40, gambling, ordinary income). If treated as gambling, from 2026 the OBBBA caps loss deductions at **90%** of losses (win $20k/lose $20k → tax on $2k phantom income) and losses require itemizing. — [Monaco CPA](https://www.monacocpa.cpa/post/prediction-market-taxes-kalshi-polymarket-robinhood); [Camuso CPA](https://camusocpa.com/kalshi-tax-reporting/); [Coselite](https://coselite.com/blog/kalshi-taxes)
- **Regulatory — New York [OFFICIAL/NEWS]:** NY Gaming Commission cease-and-desist to Kalshi Oct 24, 2025; Kalshi sued; SDNY denied Kalshi's preliminary injunction/TRO and later an injunction pending appeal; NY Governor/AG sued Kalshi for "illegal gambling operation" (press release 2026; CNBC dated Jul 31, 2026). — [NY AG press release](https://ag.ny.gov/press-release/2026/governor-hochul-and-attorney-general-james-announce-new-york-has-sued-kalshi); [CNBC Jul 31 2026](https://www.cnbc.com/2026/07/31/new-york-sues-kalshi-claims-it-is-illegal-gambling-operation.html); [Courthouse News](https://www.courthousenews.com/kalshi-loses-bid-to-stop-new-york-from-regulating-prediction-markets/). Snippets conflict on whether the NY AG suit was January or July 2026.
- **Regulatory — other states [NEWS]:** Nevada and New Jersey C&Ds (Mar 2025); 3rd Circuit (Apr 2026) majority held CEA likely preempts NJ law. — [The Block](https://www.theblock.co/post/348767/kalshi-sues-nevada-new-jersey-gaming-boards-after-receiving-orders-to-cease-sports-contracts); [Detroit News Apr 6 2026](https://www.detroitnews.com/story/business/2026/04/06/new-jersey-cant-regulate-kalshi-prediction-market-appeals-court-rules/89484816007/)
- **Wash-trading scrutiny [NEWS]:** CFTC reportedly weighed a probe into repeating $5,500 trades in Kalshi ETH perps; Kalshi denied wash trading. — [Unchained](https://unchainedcrypto.com/cftc-weighs-enforcement-probe-into-kalshis-ether-perps-over-repeating-5500-trades/)

### Inferences
- For a **New York** trader, regulatory shutdown risk on Kalshi sports is concrete and live (state lost injunction bid in SDNY; state suing). Unlike NJ (3rd Cir. win for Kalshi), NY is in the 2nd Circuit with an adverse district ruling so far. Venue diversification (Novig/ProphetX now CFTC-approved; Polymarket US) matters but those venues may face the same state arguments.
- Outage handling (fills during outages later refunded to counterparties) implies a bot's "good" fills during venue incidents may be reversed — P&L accounting and risk systems must tolerate trade busts.
- Name/team mapping errors: no first-hand incident found, but cross-venue arb inherently requires mapping differing market definitions/resolution rules (flagged as leg/resolution risk).

### Gaps
- No first-hand reports found on KYC/withdrawal freezes or account restrictions of bot traders on Kalshi/Novig/ProphetX/Sporttrade.
- No concrete incident of a sports market mis-settlement on Kalshi found.

## 5. Tooling and costs vs profits

### Takeaway
Data costs span two orders of magnitude: hobby odds APIs ($20-$199/mo) vs sharp/low-latency feeds (Unabated from ~$3,000/mo; OpticOdds reportedly ~$5,000/mo per sport, unverified). Bot hosting is cheap (serverless/VPS); the binding costs are data, fees, and time. Vendors selling Kalshi bot platforms (~$99/mo) are a large share of "how to" content.

### Cited Findings
- The Odds API: Free 500 calls/mo; $20 (20k); $49 (90k); $99 (4.5M); $199 (12M calls/mo). — [SportsAPI.com directory](https://sportsapi.com/api-directory/the-odds-api/); [APIs.io](https://apis.io/plans/the-odds-api/the-odds-api-plans-pricing/)
- Unabated API: personal use from **$3,000/month**. — [Unabated API](https://unabated.com/get-unabated-api); [OddsPapi pricing comparison](https://oddspapi.io/blog/odds-api-pricing-2026-comparison/)
- OpticOdds: no public pricing; community-reported (unverified) ~**$5,000/month per sport**; OddsJam's developer API runs on OpticOdds. OddsJam consumer tiers ~$199/mo (Gold), ~$999/mo (Platinum). — [OddsPapi OddsJam alternative](https://oddspapi.io/blog/oddsjam-api-alternative/); [ArbBets OddsJam pricing](https://getarbitragebets.com/blog/oddsjam-pricing)
- TheRundown publishes API pricing tiers. — [TheRundown API pricing](https://therundown.io/pricing/api)
- Betfair Live App Key one-off £499. — [Betfair Developer Program](https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access)
- Open-source stacks: Python, XGBoost, Modal (serverless), Supabase. — [GitHub Rohan5commit](https://github.com/Rohan5commit/kalshi-sports-bot)
- **[PROMO]** Hosted Kalshi bot platforms: $99/mo. — [BotForKalshi](https://www.botforkalshi.com/); [KalshiBot](https://kalshibot.com/). VPS vendors (QuantVPS, TradoxVPS) publish Polymarket "how to win" content. — [QuantVPS](https://www.quantvps.com/blog/polymarket-hft-traders-use-ai-arbitrage-mispricing)

### Inferences
- Rough cost math: with a $3,000-5,000/mo sharp feed, a $100k bankroll must return ~36-60%/yr just to cover data; a pregame bot on $99-199/mo odds APIs is cheap but those feeds are the same public prices everyone else sees.

### Gaps
- No practitioner reports of total monthly running costs vs realized profit were found.

## 6. Survivorship bias and realistic expectations for a solo developer with ~$100k

### Takeaway
The weight of verifiable evidence says most participants lose and profits concentrate in a tiny, well-resourced slice; public "bot made $X" stories are survivor-selected, often crypto rather than sports, and often attached to vendors selling bots/VPS. Realistic expectations for a solo pregame-sports dev should be modeled on small-edge, capacity-limited returns with nontrivial probability of zero or negative results, plus venue/regulatory interruption.

### Cited Findings
- 84.1% of Polymarket wallets lose; 2% ever >$1k; 0.015% sustain ≥$5k/mo for 4 months. — [The Defiant](https://thedefiant.io/news/research-and-opinion/polymarket-profitability-report-april-2026)
- Betfair: <0.5% of customers profitable enough to pay the 2008 Premium Charge; <0.1% hit the 2011 higher rates. — [Wikipedia: Betting exchange](https://en.wikipedia.org/wiki/Betting_exchange)
- Headline bot wins ($313 → $438k; $63 → $131k) are single-wallet, short-window, crypto 15-min stories. — [Bitget/Kriptoworld](https://www.bitget.com/news/detail/12560605137629); [Finbold](https://finbold.com/trading-bot-turns-63-into-131k-on-polymarket-in-a-month/)
- Clickbait/promo genre examples: "Claude AI Trading Bots Are Making Hundreds of Thousands on Polymarket" ([Medium](https://medium.com/@weare1010/claude-ai-trading-bots-are-making-hundreds-of-thousands-on-polymarket-2840efb9f2cd)); "How AI Trading Bots Are Making Millions on Polymarket" ([andrew.ooo](https://andrew.ooo/posts/ai-trading-bots-polymarket-profits/)); counterpoint "Debunking the 'Polymarket Dream'" ([Medium](https://fglancszpigel.medium.com/debunking-the-polymarket-dream-d67ba3922e4b), not readable here).

### Inferences
- For a NY pregame-US-sports trader: (1) latency strategies are mostly irrelevant/unwinnable; (2) edge must come from better pricing vs sharp consensus or cross-venue discrepancies net of ~1.75¢ taker fees at 50¢; (3) depth/capacity on pregame markets and competition from 2,000+ bot makers and designated MMs cap scalable profit; (4) NY legal status of Kalshi sports is the single largest tail risk; (5) 2026 tax treatment could turn a breakeven year into a tax bill if gambling treatment applies.

### Gaps
- No credible distribution of outcomes (win/lose counts) for sports-specific bot builders; Reddit self-reports could not be accessed.
- No figures on realistic annual ROI for a $100k pregame sports bot on US exchanges from any verifiable source.
