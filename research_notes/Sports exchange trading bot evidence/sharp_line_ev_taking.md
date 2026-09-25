# +EV "Taking" vs De-Vigged Sharp Lines (Pinnacle/Circa) on Exchanges & Prediction Markets — Evidence Notes (2024-2026 + academic background)

> Method note: WebFetch was blocked by the network egress proxy for every host tried (arxiv.org, gwu.edu, cepr.org, football-data.co.uk, pinnacleoddsdropper.com, sportsbookish.com, pith.science; reddit.com CONNECT also refused). All findings below come from search-result snippets/abstracts, not full-text reads. Numbers are quoted as the snippets gave them; treat anything not from a paper abstract as moderate-to-low reliability. Reddit/Discord practitioner threads could not be accessed directly.
>
> Reliability tags: **[A]** peer-reviewed / academic working paper; **[B]** reputable industry source or primary operator; **[C]** affiliate/aggregator/marketing blog or social post — directional only.

## 1. Are Pinnacle / sharp closing lines efficient, and which de-vig method is best calibrated?

### Takeaway
Academic and industry evidence consistently treats the Pinnacle (and, for US football/props, Circa) closing line as the best available probability estimate, and "beating the de-vigged sharp consensus" has been shown to be profitable at soft books (Kaunitz et al.). For de-vigging, methods that model favourite-longshot bias (power/logarithmic, Shin, odds-ratio) beat naive multiplicative normalisation, but on low-margin Pinnacle 2-way markets the methods differ by only ~0.1–0.5 pp on average — small relative to a 2.5% threshold, except at heavy favourites/longshots (up to ~2.4 pp).

### Cited Findings
- **Kaunitz, Zhong & Kreiner (2017), "Beating the bookies with their own numbers"**: strategy used the consensus of bookmaker odds as the "fair" probability and bet mispriced outliers; backtest on historical closing odds returned **3.5% over 56,435 bets**, 10.82 SD above random-bet returns (p < 1 in a billion); live paper/real-money trading returned **8.5% on 265 bets over 5 months ($957.50 profit)**, after which several bookmakers **limited their accounts**. [A] — [arXiv 1710.02824](https://arxiv.org/abs/1710.02824); [Semantic Scholar](https://www.semanticscholar.org/paper/Beating-the-bookies-with-their-own-numbers-and-how-Kaunitz-Zhong/d65ec3c62643efdd91003f0710bc87e5cdcf6455); code: [GitHub BeatTheBookie](https://github.com/Lisandro79/BeatTheBookie)
- Joseph Buchdahl ("Wisdom of the Crowd", updated) argues that because Pinnacle's model is based on high turnover, its closing price in large markets is "the best measure of the true probability"; he formalised the de-vig methods "weights proportional to the odds", odds-ratio (Cheung 2015) and logarithmic/power, and calls equal-margin "crude and frankly inaccurate". [B] — [football-data.co.uk PDF](https://www.football-data.co.uk/The_Wisdom_of_the_Crowd_updated.pdf); method provenance per [R `implied` package docs](https://cran.r-project.org/web/packages/implied/vignettes/introduction.html)
- **Štrumbelj (2014), IJF 30(4):934–943**: probabilities from Shin's model are more accurate forecasts than basic normalisation or regression; some bookmakers are significantly better probability sources than others, and **betting-exchange odds are not always the best source, especially in smaller markets**. [A] — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0169207014000533)
- **Clarke, Kovalchik & Ingram (2017)**: power method (raise implied probs to a common exponent) never yields probabilities outside 0–1, allows for favourite-longshot bias, and on three large bookmaker datasets across sports "universally outperformed the multiplicative method and outperformed or was comparable to the Shin method". [A] — [Clarke et al. PDF](https://outlier.bet/wp-content/uploads/2023/08/2017-clarke-adjusting_bookmakers_odds.pdf); [ResearchGate](https://www.researchgate.net/publication/326510904_Adjusting_Bookmaker's_Odds_to_Allow_for_Overround)
- On Pinnacle closing prices, multiplicative / power / Shin differ by **0.1–0.5 probability points on average and up to 2.4 points at the extremes**, with disagreement growing as favourites get stronger; because Pinnacle margins are ~1–3%, methods converge. [C] — [SharkBetting devig explainer](https://www.sharkbetting.com/blog/devig-explained)
- Pinnacle's own position: consistently beating its closing line is "the best indicator of [a] winning bettor", and winners are not restricted. [B, self-interested] — [Pinnacle Betting Resources](https://www.pinnacle.com/betting-resources/en/betting-strategy/what-distinguishes-winning-from-losing-bettors/6bt2g5bmd43myskt)
- Unabated builds its fair line from a sport-specific blend of market-making books "calibrated... based on how quickly and accurately each source reaches the closing line" — i.e. practitioners weight sources by speed-to-close, not Pinnacle alone. [B] — [Unabated Line](https://unabated.com/tools/core/unabated-line)
- Favourite-longshot bias is also present in prediction markets (Kalshi: see §4; Polymarket: a 2026 arXiv paper "The Favorite-Longshot Bias in Prediction Markets: Evidence from Polymarket"). [A, title only seen] — [arXiv 2609.12878](https://arxiv.org/pdf/2609.12878)

### Inferences
- For 2-way US pregame markets priced near 40–60%, de-vig choice is second-order (<0.5 pp); for heavy favourites/longshots (e.g. ML −300+ or props), the method can move "edge" by 1–2+ pp, enough to create or erase a 2.5% threshold. Use power or Shin (or an ensemble) and require extra margin at extreme prices.
- Kaunitz's result is the canonical proof that consensus-vs-outlier taking works *against soft books*; the live sample (265 bets) is small, and the sustainability problem there was limits, which exchanges remove — but the exchange version faces fees and informed counterparties instead.

### Gaps
- Could not read full text of Buchdahl's paper to extract his ranking of methods on out-of-sample log-loss, or his value-betting yields vs expected.
- No peer-reviewed study found specifically testing Pinnacle closing-line efficiency for US sports (NFL/NBA/MLB/NHL) in 2024-26; most academic work is European football.

## 2. How large are realistic edges; how often are >2.5% edges real vs stale; what fraction of +EV bettors beat CLV?

### Takeaway
Practitioner consensus is that genuine +EV vs sharp consensus lives mostly in the +1% to +6% range, and double-digit "edges" are usually stale or erroneous data. There is no credible published statistic on what share of +EV bettors beat CLV long term; claims circulating on affiliate blogs are unsourced.

### Cited Findings
- "Most real +EV opportunities against the market consensus fall in the +1% to +6% range. Anything dramatically higher usually means the line is stale or the win probability estimate is wrong — treat double-digit EV with skepticism." [C] — [search snippet from +EV tool guides, e.g. OddsIndex/OddsShopper](https://oddsindex.com/guides/expected-value-calculator)
- User complaints that OddsJam "consistently displays inaccurate lines", misdisplays persisting "for weeks" — false positives from data errors are a known operational problem. [C, user reviews] — [Trustpilot OddsJam](https://www.trustpilot.com/review/oddsjam.com)
- Common OddsJam complaints: price, **shared edges** (many subscribers hitting the same alert), and account limiting. [C] — [RotoWire OddsJam review](https://www.rotowire.com/betting/oddsjam-review)
- Claims that "bettors who beat Pinnacle's close by 2% are profitable long-term" and "2025 Pinnacle data shows punters who consistently beat the closing spread by 0.5–1 point win over 52% of bets vs 48%" appear on affiliate sites with no traceable dataset. [C — treat as unverified] — [XCLSV CLV guide](https://xclsvmedia.com/closing-line-value-clv-explained-the-complete-guide-for-sports-bettors-in-2026/); [AsianOdds CLV](https://asianodds.com/en/closing-line-value)
- Buchdahl has written on CLV measurement (article summarised on Pinnacle Odds Dropper); full text not accessible. [B] — [PinnacleOddsDropper / Buchdahl CLV](https://www.pinnacleoddsdropper.com/blog/closing-line-value--clv-demystified-by-expert-joseph-buchdahl)
- Kaunitz et al.'s backtest yield (3.5%) is the best academic anchor for "realistic edge" when taking outliers vs consensus, and that was vs soft European books with larger margins than US exchanges. [A] — [arXiv 1710.02824](https://arxiv.org/abs/1710.02824)

### Inferences
- A >2.5% raw edge vs de-vigged Pinnacle on an exchange is in the plausible band but near its bottom; after fees (§4) many such edges go to zero or negative. Edges well above ~6–8% should be presumed stale/erroneous (wrong line, wrong market mapping, suspended Pinnacle line, injury news) until verified.
- With ~2–3% true edge and ~50% win prob, noise dominates for thousands of bets; CLV (vs Pinnacle close) is the only feasible short-run validation metric.

### Gaps
- No reliable data on % of +EV bettors who beat CLV long-term, or on false-positive rates of +EV alerts. Reddit (r/sportsbook, r/algobetting) was inaccessible.

## 3. Latency, stale prices, bots, and odds-data feeds

### Takeaway
Prediction-market/exchange sports books are largely quoted by market makers who mark off the same sharp sportsbook (Pinnacle/PS3838), so "stale" exchange prices after a Pinnacle move are exactly what professional MMs and snipers race to fix or pick off. Retail-grade polled odds APIs (≥1s latency, often much more) are not competitive for that race; streaming feeds are sub-second.

### Cited Findings
- On Kalshi, designated market makers "compete on spread and on quoting uptime" (program obligations such as quoting 98% of every hour in a series), "but nobody competes on the level... They are all marking from the same sportsbook." [C/B, Substack analysis] — [Veriphix: "The Price Nobody Set"](https://veriphix.substack.com/p/the-price-nobody-set)
- Susquehanna (SIG) was Kalshi's first dedicated institutional market maker (Apr 2024) and provides liquidity on single-event sports books. [B] — [BusinessWire](https://www.businesswire.com/news/home/20240403664852/en/Kalshi-Onboards-Its-First-Dedicated-Institutional-Market-Maker); [Sportico](https://www.sportico.com/business/sports-betting/2025/kalshi-parlay-combo-rfq-explainer-1234877038/)
- For Polymarket sports, "the sharp reference is the same as for Kalshi: Pinnacle (PS3838)"; MM guides warn adverse selection (informed traders picking off stale quotes) "can vaporize months of rebate income in a single market". [C] — [StartPolymarket MM guide](https://startpolymarket.com/strategies/market-making/)
- Polymarket has a **250ms taker delay** on sports markets (limits pure intra-venue latency sniping) but cross-venue arbs vs Kalshi/sportsbooks are unaffected; arb bots monitor Kalshi + Polymarket or Pinnacle/PS3838 and fire when divergence exceeds fees. [C] — [Claw Arbs Polymarket arb bot](https://clawarbs.com/blog/polymarket-arbitrage-bot/)
- Polymarket began piloting a market-order (taker) fee in sports on **18 Feb 2026**. [B/C] — [Pine Analytics Substack](https://pineanalytics.substack.com/p/polymarket-fee-rollout)
- Odds feeds: OpticOdds markets sub-second (sub-800ms) streaming with SLAs; Odds-API.io claims WebSocket sub-100ms; **The Odds API is REST polling only** — at 1 req/s effective latency ≥1,000ms. [C, vendor comparisons] — [Odds-API.io vs OpticOdds](https://odds-api.io/blog/odds-api-vs-opticodds); [SharpAPI comparison](https://sharpapi.io/compare/real-time-odds-api); [OddsPapi polling vs websockets](https://oddspapi.io/blog/how-often-odds-apis-update/)
- "Update frequency is a property of the market, not the feed": quiet pregame lines may not move for an hour; 1–5s polling fine for display only. [C] — [OddsPapi](https://oddspapi.io/blog/how-often-odds-apis-update/)
- In-game Kalshi favourite prices were reported 5–15 pp below the 14-book no-vig median (e.g. 75¢ vs 88%), which the author attributes to exchange-specific factors rather than exploitable lag. [C] — [SportsBookISH research](https://sportsbookish.com/research/why-mid-game-kalshi-lines-lag)
- Stale-line betting discussion (when books update at different speeds, one can briefly offer off-market prices). [C] — [hagrin, Medium](https://hagrin.medium.com/betting-into-bad-stale-lines-ec477e7c0b96)

### Inferences
- A cross-venue "Pinnacle moved, exchange didn't" strategy competes against SIG-type MMs and arb bots reading Pinnacle directly. If your Pinnacle price comes via a polled aggregator with seconds of delay, surviving "edges" are adversely selected: the stale quotes you can still hit are disproportionately those where your *reference* is stale (e.g. Pinnacle about to move the other way, or already moved and your feed hasn't updated).
- "Who moved first" filter: only take when the exchange price is off a Pinnacle price that has been stable for N seconds/minutes and the exchange quote is the one that moved (or has been static and is resting from a retail maker). Reject when Pinnacle just moved (race lost) or when the exchange moved first (possible informed flow / news).

### Gaps
- No measured statistics found on Kalshi/Novig/Polymarket update lag (in seconds) after Pinnacle moves, nor on bot fill rates. Pricing of OpticOdds/OddsJam API not captured (enterprise, quote-based).
- Novig and ProphetX fee/commission schedules and MM arrangements not verified.

## 4. Is Pinnacle still the sharpest reference; do prediction markets lead or lag?; fees

### Takeaway
Pinnacle remains the default global reference for mainline US markets; Circa is frequently cited as leading on NFL and props in the US. Prediction markets mostly *follow* sportsbooks (MMs mark off them). Kalshi pricing shows a favourite-longshot bias and its taker fee (0.07·p·(1−p)) alone exceeds 2.5% of stake near even money, so a 2.5% raw edge threshold is insufficient on Kalshi as a taker.

### Cited Findings
- Practitioner view: Pinnacle sharper for mainlines (ML/spreads), Circa sharper for props; for US markets, Circa "frequently leads the number that other regulated books follow". [C] — [@EVBettors on X](https://x.com/EVBettors/status/1967060456184705390); [ValueBetFactory sharpest books](https://valuebetfactory.com/betting-education/sharpest-sportsbooks); [Pikkit sharpest books](https://pikkit.com/blog/which-sportsbooks-are-sharp)
- Practitioners also claim Novig/ProphetX are "VERY sharp" when liquid (e.g. >$25k resting on a prop) — i.e. exchange prices can themselves be a sharp reference. [C] — [Alex Monahan on X](https://x.com/AlexMonahan100/status/1974164718538363018)
- **Bürgi, Deng & Whelan (2026), "Makers or/and Takers: The Economics of the Kalshi Prediction Market"** (GWU working paper 2026-001): strong favourite-longshot bias; contracts <10¢ lose >60%; contracts >50¢ earn small, statistically significant positive returns; holds across categories; bias possibly diminishing over time. Takers lose ~32% on average, makers ~10% (per snippet; likely dollar-weighted across all categories). [A] — [GWU WP](https://www2.gwu.edu/~forcpgm/2026-001.pdf); [RePEc](https://ideas.repec.org/p/gwc/wpaper/2026-001.html); [Whelan PDF](https://www.karlwhelan.com/Papers/Kalshi.pdf)
- **Kalshi taker fee = 0.07 × p × (1−p)** per contract (1.75¢ at 50¢), not refunded on wins; **makers on most sports markets pay 1/4 of taker fee**. [B] — [Karl Whelan Substack](https://finemarginskarlwhelan.substack.com/p/prediction-markets-vs-sportsbooks)
- **"Prices, Probabilities, and Parlays" (arXiv 2607.14430, 2026)**: 23 million Kalshi moneyline trades across major leagues; calibration is near-perfect mid-life but departs sharply near expiry (step-like in final 10 minutes; Prelec curvature >1); cross-game parlays systematically overpriced vs product of legs. [A] — [arXiv 2607.14430](https://arxiv.org/abs/2607.14430)
- Kalshi pregame NFL pricing after fees was reported ~8% worse than FanDuel and ~6% worse than DraftKings (methodology unclear). [C] — [SCCG Management](https://sccgmanagement.com/sccg-articles/2025/12/29/kalshi-pricing-vs-sportsbooks/)
- Kalshi-wide calibration: over 2.24M resolved markets (2021–mid-2026), Brier falls from ~0.09 at 3 months to <0.02 at close. [B, operator research] — [Kalshi Research](https://kalshi.com/research/publications/calibration)
- Vanderbilt study (Clinton & Huang 2025; 2,500 mostly political markets): Polymarket 67%, Kalshi 78%, PredictIt 93% "accuracy" — not sports, not calibration. [A-ish, secondary report] — [DL News](https://www.dlnews.com/articles/markets/polymarket-kalshi-prediction-markets-not-so-reliable-says-study/)

### Inferences
- Fee math (Kalshi taker, computed from the cited formula): at fair 50%, buying at a price 2.5% below fair (48.8¢) costs 48.8 + 1.75 = 50.5¢ → **EV ≈ −1.0%**. Taker fee as % of stake: 3.5% at 50¢, ~5.6% at 20¢, ~1.4% at 80¢. Maker fee ≈ 0.9% at 50¢. So on Kalshi a 2.5% raw threshold only works as a maker or on favourites; for takers the threshold must be fee-inclusive (roughly edge > fee% + ~1% buffer for de-vig/model error).
- The Kalshi FLB (favourites slightly positive, longshots heavily negative) means a Pinnacle-de-vigged comparison will more often flag *favourites* as cheap on Kalshi — consistent with a real, if small, structural edge, and longshots as expensive.
- Because MMs mark off sportsbooks, prediction markets should mostly lag, not lead; exceptions are thin prop markets and late-news windows where a liquid exchange order book can be ahead of a soft reference.

### Gaps
- No quantitative lead/lag (Granger-type) study of Kalshi/Polymarket vs Pinnacle for US pregame sports found.
- Sport-specific (NFL vs NBA vs MLB) breakdown of Kalshi FLB/returns not visible in snippets.

## 5. Timing (openers vs close), liquidity, late news/adverse selection

### Takeaway
Edges vs the market are generally larger early (openers, low limits, less information) and the close is most efficient; near close, exchange liquidity is highest but so is informed flow, and Kalshi calibration specifically degrades in the final minutes before settlement (a live-game phenomenon for moneylines). Hard numbers for exchanges are scarce.

### Cited Findings
- Sharp bettors target early-week openers before books adjust; an unnamed multi-season NFL analysis reported closing favourite lines ~1.2% worse value than openers and underdogs ~2.1% better, largest in high-profile games. [C — unverified dataset] — [DEV Community / edgelab](https://dev.to/edgelab/sharp-money-vs-public-money-what-betting-line-movement-data-reveals-4c4n); [Covers look-ahead lines](https://www.covers.com/nfl/how-to-use-lookahead-lines)
- Academic: information asymmetry in the NFL market — inside information vs informed bettors (ScienceDirect, 2022). [A, abstract only] — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S2214635022000806)
- Kalshi calibration departs sharply as expiry approaches (final ten minutes step-like). [A] — [arXiv 2607.14430](https://arxiv.org/abs/2607.14430)
- Practitioner claim: Novig can carry >$25k liquidity on a single prop side when active. [C] — [Alex Monahan on X](https://x.com/AlexMonahan100/status/1974164718538363018)
- MM guides: leaving quotes up through news lets informed traders fill stale orders. [C] — [StartPolymarket](https://startpolymarket.com/strategies/market-making/)

### Inferences
- A de-vigged-Pinnacle taker strategy is structurally a "close-chasing" strategy (it needs a sharp reference, which is sharpest near close). Opener-time comparisons use a softer Pinnacle line (lower limits, more movement), raising false-positive risk; near-close comparisons are more reliable but the edges are smaller and more contested.
- Late-news adverse selection is the main risk: a cheap exchange price vs Pinnacle may exist because a counterparty knows about a lineup/injury that Pinnacle has not yet priced (or your feed has not yet shown). Lineup-lock windows (NBA ~30 min pre-tip, MLB lineups/pitcher scratches, NHL goalies) deserve blackout or stricter thresholds.

### Gaps
- No exchange-specific data on edge size by time-to-start or fill depth near close.

## 6. Reported results from +EV bettors/bots and migration to exchanges

### Takeaway
Social-media and marketing sources say exchanges (Novig, ProphetX, Sporttrade, Kalshi, Polymarket) welcome winners and are now core to +EV/arbing rotations because they don't limit; verified P&L records are absent. The documented failure mode for +EV at sportsbooks is limiting (Kaunitz), whereas on exchanges it is fees, thin liquidity, and competition from MMs/bots.

### Cited Findings
- "This is exactly how ProphetX works. Sharp lines. No limits. You bet against other people... If you're doing any kind of value betting or arbing, not having an exchange in your rotation is leaving money on the table." [C] — [@EVBettors on X](https://x.com/EVBettors/status/2038743569184243892)
- Novig markets "no limits on winners"; exchanges earn from commissions not bettor losses. [C] — [@shanderbets on X](https://x.com/shanderbets/status/1910326583606333504)
- Kaunitz et al. accounts limited after only 265 live bets / 5 months of +8.5%. [A] — [arXiv 1710.02824](https://arxiv.org/abs/1710.02824)
- A Substack commenter argued there "should be enough free lunch where picking off the resting orders should be good regardless of the fees" on these exchanges (anecdotal, unverified). [C] — [Closing Line Substack sitemap/threads](https://closingline.substack.com/sitemap/2025)
- Retail takers on Kalshi lose ~32% on average vs makers ~10% (all categories) — the population average taker is heavily negative. [A] — [Whelan et al.](https://ideas.repec.org/p/gwc/wpaper/2026-001.html)

### Inferences
- The migration logic (no limits) is sound, but it removes the one constraint that made soft-book +EV profitable-per-bet while adding fees and professional counterparties; a sharp-reference taking bot on exchanges is essentially competing with the MMs' own pricing source.
- Sustainable edge is more plausible where (a) fees are low (maker orders; Novig/ProphetX if commission is low), (b) the counterparty is a retail resting order rather than an MM, (c) markets are thin/secondary (props, WNBA, NHL) where MMs quote wider, and (d) the Pinnacle line is stable and liquid. It is least plausible in main NFL/NBA sides/totals near close on Kalshi as a taker.

### Gaps
- No audited or verifiable P&L from exchange +EV bots found; Reddit threads (r/algobetting, r/sportsbook, r/Kalshi) inaccessible from this environment.
- No data on whether Novig/ProphetX/Sporttrade restrict API/bot takers or use speed bumps.
