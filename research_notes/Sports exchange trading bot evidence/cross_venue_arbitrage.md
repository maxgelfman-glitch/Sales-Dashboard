# Cross-Venue Arbitrage on Sports Prediction Markets and Exchanges (Kalshi, Polymarket, Novig, ProphetX, Sporttrade, Betfair, sportsbooks), 2024-2026

Method note: The research environment's egress proxy blocked direct page fetches (arxiv.org, reddit.com, substack, oddsshopper, pillarlab, yashkothari.ca all returned EGRESS_BLOCKED). All findings below come from search-engine result summaries/snippets of the cited pages, not from reading the full pages. Numbers should be spot-checked against the originals before being relied on. Reliability labels: [ACADEMIC] peer-reviewed or preprint; [DATA] measured but non-academic; [VENDOR] arb-tool / affiliate / SEO marketing content; [ANECDOTE] single-person report; [NEWS].

## 1. How often do cross-venue gaps appear, how big, how long, how deep?

### Takeaway
Measured cross-venue price differences between Kalshi and Polymarket are real and persistent at the "quoted price" level (2-4% average execution-aware deviation in the best academic study), but the executable, fee-surviving, two-leg-fillable part is small, brief (seconds), and competed for by bots; sports-specific cross-venue academic measurement is essentially absent, and the one sports-specific academic study (Polymarket NBA, single venue) found arbitrage extremely rare.

### Cited Findings
- [ACADEMIC] Gebele & Matthes, "Semantic Non-Fungibility and Violations of the Law of One Price in Prediction Markets" (arXiv 2601.01706, Jan 2026): 100,000+ events across ten venues, 2018-2025; roughly 6% of all events are concurrently listed across platforms; semantically equivalent markets show persistent execution-aware price deviations of 2-4% on average "even in highly liquid and information-rich settings"; attributes gaps to structural frictions (resolution semantics, institutional segmentation, limits to arbitrage) rather than informational disagreement; arbitrage "becomes capital-intensive or unenforceable." — [arXiv abstract](https://arxiv.org/abs/2601.01706)
- [ACADEMIC] Same authors' data: cross-platform duplication rose sharply from early 2024 and expanded into sports; by 2025 windows routinely exceed 1,200-1,500 matched pairs. — [arXiv HTML (via search summary)](https://arxiv.org/html/2601.01706v1)
- [ACADEMIC] Yang, Cheng, Zou, "Arbitrage Analysis in Polymarket NBA Markets" (arXiv 2605.00864 / SSRN 6624718): 75M+ order-book snapshots, 173 NBA games, Feb 4-Mar 4 2026. Single-market (YES+NO<1) arbitrage "exceedingly rare": only 7 executable in-game episodes, median duration 3.6 seconds. Combinatorial arbs across linked markets (spread vs moneyline) arise because books are isolated. Single venue only, not cross-venue. — [arXiv](https://arxiv.org/abs/2605.00864); [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6624718)
- [ACADEMIC] Saguillo, Ghafouri, Kiffer, Suarez-Tangil, "Unravelling the Probabilistic Forest" (AFT 2025, arXiv 2508.03474): ~$40M realized arbitrage profit on Polymarket alone, Apr 2024-Apr 2025, 86M bets, 7,000+ markets with measurable mispricing (intra-market rebalancing and combinatorial). Within Polymarket, not cross-venue; dominated by non-sports (election-period) activity per secondary coverage. — [arXiv](https://arxiv.org/abs/2508.03474); [PDF](https://suarez-tangil.networks.imdea.org/papers/2025aft-arbitrage.pdf)
- [ACADEMIC] Gebele & Mutzel, "Executable Arbitrage and Market Efficiency in Prediction Markets" (arXiv 2608.00666, Aug 2026): distinguishes payoff-space no-arbitrage from "protocol-executable" no-arbitrage; on Polymarket neg-risk markets estimates $1.12M arbitrage profit ($1.086M converter-enabled, only $32K from settlement-based basket formation), consistent with pre-settlement conversion reducing capital lock-up. Implication: arbs that must be held to settlement (like cross-venue ones) are far less exploited. — [arXiv](https://arxiv.org/abs/2608.00666)
- [VENDOR/SEO] Cross-platform Kalshi-Polymarket arb "typical pre-cost spreads of 1.5%-4.5% per trade, windows lasting 2-7 seconds (AhaSignals, 2026)." Vendor-sourced. — [Turbine blog / search summary](https://www.turbinefi.com/blog/prediction-market-arbitrage-bots-2026)
- [VENDOR/SEO, unverified] "Cross-platform arbitrage windows between Kalshi and Polymarket collapsed from an average of 12.3 seconds in 2024 to 2.7 seconds in Q1 2026"; "73% of profits go to sub-100ms bots"; "only about 1% of detectable arbs in U.S. election markets was actually executed." No primary data located; treat as marketing. — [Turbine latency blog](https://www.turbinefi.com/blog/prediction-market-arbitrage-latency-speed-2026); [NYC Servers guide](https://newyorkcityservers.com/blog/prediction-market-arbitrage-guide)
- [VENDOR] Another claim: "Arbitrage gaps close within 15 to 30 seconds; value bets vs sharp lines live 30-90 seconds." Conflicts with the 2.7s figure above. — [ClawArbs](https://clawarbs.com/prediction-markets/)
- [VENDOR/SEO] 2026 World Cup: "documented gaps of 5 to 8 percentage points" between Polymarket and Kalshi on same team's winner odds; example 55c vs 68c = 13c "guaranteed" profit. Likely futures/outright markets (long lockup), no methodology; the example is illustrative. Also claims gaps "persist for hours, sometimes days." — [SailGP prediction markets guide](https://sailgp.com/prediction-markets/guide/kalshi-vs-polymarket/arbitrage)
- [NEWS/affiliate] NBA playoffs 2026: sharp bettors cited 1-2c Knicks premium on Kalshi over Polymarket; strategy locks "1-3c per dollar"; example 64c YES + 35c NO = 99c cost, 1c gross before fees. — [XCLSV](https://xclsvmedia.com/kalshi-vs-polymarket-arbitrage-2026-nba-finals-sharp-bettors/); [PredictionNews on Finals pricing flip](https://predictionnews.com/story/kalshi-and-polymarket-price-spurs-vs-knicks-nba-finals-differently-4627d281)
- [ANECDOTE] Developer (realfishsam, pmxt.dev) reports "consistent 2-5% price spread" and a bot capturing 1.5-4.5% spreads, but on Fed-rate and election markets, not sports. — [DEV Community](https://dev.to/realfishsam/how-i-built-a-risk-free-arbitrage-bot-for-polymarket-kalshi-4f); [GitHub](https://github.com/realfishsam/prediction-market-arbitrage-bot)
- [NEWS] WSJ-reported anecdote: a trader made $500 in ~45 seconds on a brief mispricing in Vikings-victory contracts on Kalshi (single-venue scalp, not locked two-leg arb). — [WSJ via iTiger](https://www.itiger.com/hant/news/2570847487)
- Tools/scanners that exist (existence only, no performance data): Apify "Polymarket + Kalshi Arbitrage Finder" — [Apify](https://apify.com/congism/polymarket-kalshi-arb-finder); PredictionHunt arb scanner — [predictionhunt.com](https://www.predictionhunt.com/arbitrage); PredictionMarketsPicks arb scanner — [link](https://predictionmarketspicks.com/tools/arb-scanner); AhaSignals / ArbBets / Oddpool compared — [XCLSV](https://xclsvmedia.com/best-prediction-market-arbitrage-tools-2026-ahasignals-arbbets-oddpool/); OddsJam tracks Kalshi/Polymarket/ProphetX/Novig traders — [OddsJam](https://oddsjam.com/prediction/traders); GitHub bots ImMike/polymarket-arbitrage (10,000+ markets), TopTrenDev (Rust), CarlosIbCu (BTC hourly) — [ImMike](https://github.com/ImMike/polymarket-arbitrage), [TopTrenDev](https://github.com/TopTrenDev/polymarket-kalshi-arbitrage-bot), [CarlosIbCu](https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot); "30+ open-source Kalshi bot projects" — [botforkalshi](https://www.botforkalshi.com/blog/open-source-kalshi-bot-ecosystem)
- OddsJam is built around sportsbooks and (per a competitor) does not continuously scan Polymarket/Kalshi/Novig orderbooks. — [ArbBets (competitor, biased)](https://getarbitragebets.com/blog/oddsjam-alternatives)

### Inferences
- Pregame sports game-winner markets on Kalshi and Polymarket are the most liquid and most bot-watched; persistent multi-cent gaps are more plausible in futures/outrights (World Cup, NBA title) where lockup is weeks-months, which kills annualized return.
- The 2-4% academic deviation is an average across all categories and venues, not specifically executable US sports pregame gaps; it does not by itself imply profit after fees for taker-taker execution.
- No source provides depth (contracts available at the arb price) for sports cross-venue gaps. Order-book depth at the gap price is likely the binding constraint.

### Gaps
- No published dataset measuring Kalshi vs Novig / ProphetX / Sporttrade sports price gaps, frequency, or depth.
- Could not verify the 12.3s -> 2.7s window-compression statistic or the "1% executed" statistic; primary data not found.
- Full text of arXiv papers not accessible (egress blocked) for sports-specific breakdowns.

## 2. How do fees change the math? Do fees usually eat the gaps?

### Takeaway
At near-50/50 game prices, taker-taker fees on Kalshi (~1.75c/contract) plus Polymarket US (~1.5c/contract) total ~3.25c per $1 pair, which exceeds the 1-2c gaps typically cited for liquid sports games; only maker-side execution, ProphetX/Novig legs, or larger gaps (usually in less liquid/longer-dated markets) leave a positive margin.

### Cited Findings
- Kalshi taker fee = roundup(0.07 x C x P x (1-P)); at 50c, 100 contracts ~ $1.75. Maker orders (where charged) use 0.0175 coefficient, ~ $0.44 per 100 at 50c. — [Laika Labs fee comparison](https://laikalabs.ai/prediction-markets/kalshi-vs-polymarket-fees-comparison); [SI](https://www.si.com/prediction-markets/reviews/kalshi-vs-polymarket)
- Polymarket (international) introduced probability-based taker fees for Sports on March 30, 2026, peak effective ~0.75% at 50/50 per one source; another source cites a 0.0175 coefficient / ~0.44% peak for sports (conflicting). Makers pay zero. — [search summary of fee guides](https://www.rivermarkets.com/insights/kalshi-vs-polymarket.html); [Pine Analytics on fee rollout](https://pineanalytics.substack.com/p/polymarket-fee-rollout); [Yash Kothari](https://www.yashkothari.ca/writing/polymarket-kalshi-arbitrage)
- Polymarket US: fee = theta x C x P x (1-P), taker theta 0.06 (~$1.50 per 100 contracts at 50c), July 2026 schedule; slightly cheaper than Kalshi for takers. — [Laika Labs](https://laikalabs.ai/prediction-markets/kalshi-vs-polymarket-fees-comparison)
- ProphetX: 2% commission on net winnings per market (1.5% VIP tier); no fee on parlays. — [ProphetX help center](https://prophethelp.zendesk.com/hc/en-us/articles/26975338569489-Understanding-Commission); [OddsAssist](https://oddsassist.com/prediction-markets/prophetx-fees/)
- Betfair (reference market): base commission options 2%/5%/8% on net winnings, with 5%->6% and 8%->9% from June 1 2025; 2025 "Expert Fee" of 20% (GBP 25k-100k 52-week gross profit) and 40% (>GBP 100k) replaced the Premium Charge. — [Racing Post](https://www.racingpost.com/news/britain/betfair-exchange-to-introduce-new-commission-system-for-2025-as-premium-charge-is-dropped-a7wbg0v4GCAJ/); [freebets.com](https://www.freebets.com/betting-exchanges/betfair-exchange-commission-explained/)
- "Platform fees can turn a 3% gross arbitrage into a 1-2% net return, or even a loss." — [Yash Kothari (via search summary)](https://www.yashkothari.ca/writing/polymarket-kalshi-arbitrage)
- Commentary: "Kalshi is one of the worst places to bet on sports due to their high hold %." (opinion, source context unclear) — [search summary, r/ query](https://dev.to/realfishsam/how-i-built-a-risk-free-arbitrage-bot-for-polymarket-kalshi-4f)
- Sportsbooks limit winning/arb bettors; exchanges (ProphetX, Novig) and Kalshi do not limit accounts (per affiliate). — [SportsBetEdge](https://sportsbetedge.com/automation/)

### Inferences
- Worked example (my arithmetic from the formulas above, at P=0.50 both legs): Kalshi taker 1.75c + Polymarket US taker 1.50c = 3.25c. A 99c pair (1c gross) loses ~2.25c; break-even requires gap >= ~3.3c at midpoint. At P=0.80/0.20 legs fees shrink to ~1.12c + ~0.96c ~ 2.1c.
- Kalshi fee rounds up per order, so small orders pay proportionally more (e.g., 1 contract at 50c = 0.0175 rounds to 2c).
- ProphetX 2% of net winnings: buying a 50c side that wins pays ~1c per contract (2% of 50c profit) only on the winning leg; Novig (no pregame commission per the task brief, not independently verified here) and ProphetX are therefore the cheapest legs; Kalshi taker is the most expensive leg at mid-prices.
- Sportsbook legs have no explicit fee but embed 4-5%+ vig and carry limiting risk, so sportsbook-vs-exchange arbs mostly arise from stale sportsbook lines and get the sportsbook account limited.

### Gaps
- Sporttrade fee schedule and Novig's current (post-DCM, June 2026) fee schedule not confirmed from primary sources.
- No study quantifies what fraction of observed sports gaps survive fees.

## 3. Settlement-rule mismatches that break "risk-free" arbs

### Takeaway
Documented cross-venue divergences exist (notably the Feb 2026 Super Bowl halftime Cardi B market: Kalshi settled at last-traded 26c/74c, Polymarket resolved YES at $1), and sports-specific risks come from ties/draws, postponement windows, cutoff times and regulation-vs-full-game scope; no documented case of a specific named arbitrageur losing on a mainstream game-winner market was found.

### Cited Findings
- Super Bowl LX halftime "Will Cardi B perform?": Kalshi ($47.3M volume) deemed outcome ambiguous and settled at last traded price (YES $0.26, NO $0.74); Polymarket (>$10M) resolved YES at $1. A Kalshi-NO/Polymarket-YES "arb" would have been fine, but Kalshi-YES/Polymarket-NO would have recovered only 26c on a ~$1 pair. — [OddsShopper / DeFiRate via search summary](https://www.oddsshopper.com/articles/prediction-markets/kalshi-vs-polymarket-settlement-rules); [DeFiRate](https://defirate.com/prediction-markets/how-contracts-settle/)
- Political example: Kalshi paid if Connie Chan "advanced"; Polymarket paid if she got "most votes" — same-looking markets, different criteria. — [OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-vs-polymarket-settlement-rules)
- Kalshi rules carry morning-ET cutoffs vs Polymarket 11:59 PM ET windows; "on the wrong pairing both legs can lose." — [OddsShopper / search summary](https://www.oddsshopper.com/articles/prediction-markets/kalshi-postponed-game-rules)
- Kalshi postponed games: if rescheduled before contract expiration (typically ~two weeks), rescheduled result governs; a GitHub issue documents Kalshi voiding a postponed game at "fair value" (0.29+0.43+0.28=1.00) rather than refunding cost. — [OddsShopper postponed rules](https://www.oddsshopper.com/articles/prediction-markets/kalshi-postponed-game-rules); [GitHub bainluck issue #7035](https://github.com/alexander-bain/bainluck/issues/7035)
- Polymarket US publishes separate sports FAQ settlement rules (ties, postponements). — [Polymarket US docs](https://docs.polymarket.us/faqs/sports-faqs)
- Soccer: Kalshi uses a separate Draw contract for Premier League 90-minute results; "full-match scope" vs regulation mismatches cause confusion. — [PredictionMarketsPicks](https://predictionmarketspicks.com/articles/how-the-draw-contract-works-kalshi-premier-league); [DarkHorseOdds on props rules](https://about.darkhorseodds.com/guides/kalshi-polymarket-market-rules)
- Dispute recourse: Kalshi via support email/Discord; Polymarket International via $750 USDC UMA bond; no formal arbitration on either. — [OddsShopper via search summary](https://www.oddsshopper.com/articles/prediction-markets/kalshi-vs-polymarket-settlement-rules)

### Inferences
- For NFL moneylines the tie outcome (rare, ~0-2 per season) is the key mismatch: if one venue settles ties at 50/50 or refunds and the other resolves NO for both teams, a two-leg "arb" can lose. Must verify each venue's tie rule before trading.
- Postponement "fair value" settlement at Kalshi means a cost-basis arb can settle at a loss if the other venue voids at cost.

### Gaps
- No verified per-venue table of NFL tie rules for Kalshi/Polymarket/Novig/ProphetX found (fetches blocked).
- No documented, sourced instance of a named arbitrageur losing money on a sports game-winner cross-venue arb.

## 4. Execution / leg risk and bot competition

### Takeaway
Leg risk is universally named as the central practical risk; windows are reported in seconds (academic: 3.6s median for Polymarket NBA in-game single-market arbs; vendors: 2-7s cross-venue), meaning manual two-leg execution is not competitive and one-leg fills are common.

### Cited Findings
- Median 3.6-second life for executable Polymarket NBA arbs (in-game). — [arXiv 2605.00864](https://arxiv.org/abs/2605.00864)
- "You might get filled on Polymarket but stuck on Kalshi if volume dries up"; prices move between API calls. — [DEV Community (realfishsam)](https://dev.to/realfishsam/how-i-built-a-risk-free-arbitrage-bot-for-polymarket-kalshi-4f)
- "One leg fills and the other doesn't, leaving you with directional exposure" — central risk. — [Turbine](https://www.turbinefi.com/blog/prediction-market-arbitrage-bots-2026)
- CoinDesk (Feb 2026): retail traders using AI tools to exploit prediction-market "glitches" (increasing competition). — [CoinDesk](https://www.coindesk.com/markets/2026/02/21/how-ai-is-helping-retail-traders-exploit-prediction-market-glitches-to-make-easy-money)

### Inferences
- Pregame markets move slower than in-game, but the same bots watch them; the practical approach is resting a maker order on one venue and hedging on the other when filled (reduces fees but creates leg risk on the hedge).

### Gaps
- No empirical rate of one-leg fills or slippage for cross-venue sports arbs found.

## 5. Capital lockup, return on capital, deposit/withdrawal friction

### Takeaway
Cross-venue arbs require pre-funded balances on each venue and capital is locked until settlement; a 1-2c net edge on a same-day game is attractive only with high turnover, while futures arbs (e.g., title markets) lock capital for weeks/months; one anecdote found returns barely above a savings account.

### Cited Findings
- Kalshi ACH withdrawals 1-4 business days (sources vary); debit card/PayPal/Venmo/crypto ~30 minutes; only settled cash is withdrawable. — [OddsAssist](https://oddsassist.com/prediction-markets/kalshi-deposits-withdrawals-payout-speed/); [PredictionMarketsWorld](https://predictionmarketsworld.com/en-us/how-to-withdraw-from-kalshi/)
- Gebele & Mutzel: only $32K of $1.12M Polymarket neg-risk arb profit came from settlement-held baskets vs $1.086M from converter strategies that release capital early — lockup suppresses settlement-held arbitrage. — [arXiv 2608.00666](https://arxiv.org/abs/2608.00666)
- [ANECDOTE, source unclear] After fees an arb would net ~$865-$1,221 on a $50,000 position, "not significantly more profitable than parking the money in a savings account." — [search result summary; origin page not verified](https://www.mexc.com/news/97855)
- [ANECDOTE] Yash Kothari placed real money on a >20% Kalshi-Polymarket arb that resolved "a few weeks later"; payout covered his AI subscriptions for a month (i.e., small absolute dollars; not sports-specific). — [yashkothari.ca](https://www.yashkothari.ca/writing/polymarket-kalshi-arbitrage)
- Polymarket in New York: available as of Sept 2026 (US app, mobile-only for NY per one guide) but state regulators say sports prediction markets may violate NY law. — [SailGP NY page](https://sailgp.com/prediction-markets/polymarket/new-york); [Casino.org](https://www.casino.org/us/predictions/polymarket/new-york/); [TheLines](https://www.thelines.com/prediction-markets/polymarket/new-york/)
- ProphetX is sweepstakes in 40+ states; Novig limited sweepstakes states; Novig obtained DCM designation June 16 2026; Kalshi in all 50 states (affiliate claims). — [SportsBetEdge](https://sportsbetedge.com/automation/); [Lines.com](https://www.lines.com/prediction-market/prophetx-review)

### Inferences
- Illustrative (my arithmetic): 1c net on 99c pair = ~1.0% per turn; on same-day games with capital recycled daily that could compound, but capacity is limited by depth at the gap price; the same 1% on a futures market held 60 days is ~6% annualized, near T-bill yields.
- Balances must be split across venues and rebalanced via slow ACH, so effective capital needed is roughly 2x the position size plus buffer.

### Gaps
- Novig, ProphetX, Sporttrade withdrawal timings not verified.

## 6. Reported real-world P&L

### Takeaway
No credible, verified P&L from people running cross-venue sports arbitrage was found; public reports are anecdotes of small dollar outcomes, bot repos without audited results, and vendor marketing; academic P&L numbers ($40M, $1.12M) are within-Polymarket and mostly non-sports.

### Cited Findings
- $40M realized arb profit on Polymarket, Apr 2024-Apr 2025 (intra-platform, all categories). — [arXiv 2508.03474](https://arxiv.org/abs/2508.03474)
- $1.12M Polymarket neg-risk arb profit (intra-platform). — [arXiv 2608.00666](https://arxiv.org/abs/2608.00666)
- Weather-market GitHub bot reportedly earned $1,800 (not arbitrage; forecasting). — [search summary citing polymarket-kalshi-weather-bot](https://www.turbinefi.com/blog/prediction-market-arbitrage-bots-2026)
- Vendor blog "Kalshi-Polymarket Cross-Platform Arbitrage: My Exact Method and Returns" exists but content could not be read. — [PillarLab AI](https://pillarlabai.com/blog/kalshi-polymarket-cross-platform-arbitrage/)
- Launchpoly frames it as "Free Money or Myth?" (content unread). — [Launchpoly](https://launchpoly.com/blog/polymarket-kalshi-arbitrage-guide)

### Inferences
- The absence of verified sports cross-venue P&L, combined with fee math and seconds-long windows, suggests locked two-leg sports arb is a low-margin, capacity-constrained, bot-dominated activity; the more plausible edges are maker-rebate/low-fee legs (ProphetX, Novig) versus sharp-priced takers, and stale sportsbook lines (limited accounts).

### Gaps
- Reddit (r/Kalshi, r/algobetting, r/sportsbook, r/Novig) threads could not be accessed (reddit blocked by proxy); X/Twitter threads not searched.
