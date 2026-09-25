# Venue Facts for Automated Pregame Sports Trading (NY-based trader), 2025-2026

Research date: 2026-09-24/25. Method note: nearly all primary domains (docs.kalshi.com, kalshi.com, docs.novig.com, support.novig.com, docs.polymarket.us, prnewswire, ag.ny.gov, covers.com, sbcamericas.com) were blocked by the network egress proxy, so page contents could not be fetched. Findings below come mainly from search-result snippets, which cite the primary pages but may paraphrase. Reliability is marked [H]igh / [M]edium / [L]ow. Treat all numbers as needing a check against the primary docs before any bot goes live.

## 1. API: availability, feeds, rate limits, order types, matching

### Takeaway
Kalshi has the most mature and best-documented public trading API: REST plus WebSocket, token-based rate-limit tiers, FOK/IOC/GTC, and sub-penny dollar pricing. ProphetX publishes API docs aimed at algo traders and market makers. Novig's API exists, but reports conflict on how open it is. No public Sporttrade API was found. Polymarket US docs exist at docs.polymarket.us, but API details could not be checked.

### Cited Findings
**Kalshi**
- Since April 2026 Kalshi uses a token-cost rate-limit system with per-second write budgets: Basic 100, Advanced 300, Expert 600, Premier 1,000, Paragon 2,000, Prime 4,000, Prestige 8,000 tokens/sec. One order costs 10 write tokens, so Basic is about 10 orders/s, Advanced about 30/s and Premier about 100/s. Limits can be queried via `GET /account/limits` and per-endpoint costs via `GET /account/endpoint_costs`. [M: third-party summary of docs.kalshi.com/getting_started/rate_limits] — [Kalshi Rate Limits docs](https://docs.kalshi.com/getting_started/rate_limits); [botforkalshi summary](https://www.botforkalshi.com/blog/kalshi-api-rate-limits)
- CreateOrder V2 (`POST /portfolio/events/orders`) takes `time_in_force` ∈ {`fill_or_kill`, `good_till_canceled`, `immediate_or_cancel`}. The legacy `POST /portfolio/orders` endpoint (side/action/`count_fp`/`yes_price_dollars`) was deprecated and removed between June 18 and 25, 2026. [M] — [search summary of Kalshi API reference / agentbets](https://agentbets.ai/guides/kalshi-api-top-10-problems/); [vendored Kalshi API reference](https://the-obstacle-is-the-way.github.io/kalshi-starter-code-python/_vendor-docs/kalshi-api-reference/)
- Sub-penny pricing: `*_dollars` fields were added to price REST APIs on 2025-08-31 and to WebSocket messages on 2025-09-09. Quote fields such as `yes_bid_dollars`/`no_bid_dollars` were added on 2025-11-21. Legacy integer-cent fields are deprecated and may be removed, so bots must parse non-integer prices. [M] — [same sources](https://agentbets.ai/guides/kalshi-api-top-10-problems/)
- Third-party guides list "Top 10 Kalshi API problems" (auth/signing, deprecations, etc.). The content could not be fetched. — [AgentBets](https://agentbets.ai/guides/kalshi-api-top-10-problems/)

**Novig**
- Novig publishes developer docs at docs.novig.com ("Novig Dev Docs – Novig API"). [H that the docs exist; content not fetched] — [Novig Dev Docs](https://docs.novig.com/)
- A third-party summary says the API offers HTTP endpoints for orders, positions, market data and account management, plus real-time order book, trade feed and market events with "sub-second delivery". It also says API fees equal app fees (same 0.03 coefficient). [L-M, aggregator] — [oddsassist / search summary](https://oddsassist.com/prediction-markets/novig-fees/)
- Conflicting claim: "Novig has limited API access for account holders, but no public developer API for normalized prices". Some developers scrape internal endpoints or use SharpAPI. [L, odds-data vendor with commercial interest] — [SharpAPI](https://sharpapi.io/sportsbooks/novig-odds-api)

**ProphetX**
- ProphetX offers APIs to "place and manage limit orders, access real-time odds and market data, and manage your wallet, built for algorithmic traders and liquidity providers". It also has a dedicated Market Maker API. [M-H, official docs site per snippet] — [ProphetX APIs](https://docs.prophetx.co/); [Getting Started](https://docs.prophetx.co/docs/getting-started)
- ProphetX has no affiliated trading arm and relies on third-party institutional market makers. It runs a proprietary RFQ parlay mechanism. [M] — [gambling.com review](https://www.gambling.com/us/prediction-markets/reviews/prophetx); [ProphetX CFTC PR](https://www.prnewswire.com/news-releases/prophetx-obtains-cftc-approval-to-operate-americas-first-federally-regulated-sports-native-exchange-302798423.html)

**Sporttrade**: the search found no information on a public Sporttrade API.

**Polymarket US**: official docs live at docs.polymarket.us (a fee page was found; the API pages were not checked). — [Polymarket US docs](https://docs.polymarket.us/fees)

### Inferences
- For a bot, Kalshi is the lowest-friction venue: public REST and WS, documented tiers, and IOC/FOK. The Basic tier's ~10 orders/s is enough for pregame quoting across a few dozen games but not for a large quote-refresh loop.
- The June 2026 removal of the old order endpoint means older open-source Kalshi bots/SDKs may be broken.

### Gaps
- The Kalshi WebSocket channel list (orderbook_delta, ticker, trade, fill, etc.) and the read-limit tiers could not be confirmed, because docs.kalshi.com was blocked.
- Kalshi tier qualification criteria were not found.
- Kalshi matching rules were not found: whether marketable limits fill at the resting price (price improvement) is unconfirmed.
- Novig API rate limits, order types, approval requirements and matching/"fill slip" behavior were not found.
- Whether ProphetX API access for retail requires approval was not found.
- Polymarket US API order types and rate limits were not found.
- Specific outage dates were not found for any venue.
- Sports ticker series (KXNBAGAME, KXNFLGAME, KXMLBGAME, KXNHLGAME) could not be confirmed from docs.

## 2. Fees (current schedules)

### Takeaway
- **Kalshi:** takers pay 0.07·P·(1−P) per contract (rounded up); maker fees are about a quarter of that. Schedule dated July 7, 2026.
- **Polymarket US:** sports taker rate θ=0.05 with a maker rebate of −0.0125.
- **Novig:** either a small taker fee of 0.03·P·(1−P) with free makers, or no fee at all; sources conflict.
- **ProphetX:** 2% of net winnings on straights, and a taker-only fee on parlays.
- **Sporttrade:** 2% of profits.

### Cited Findings
- **Kalshi:** taker fee = ceil(0.07 × C × P × (1−P)) to the cent. The maximum is at 50¢, about $0.0175 per contract before rounding. Sports use the same 0.07 multiplier. The maker multiplier is 0.0175 (25% of taker). The official fee schedule PDF is titled "Fee Schedule for July 2026 – 7.7.26 Update". [M: snippets; PDF not fetched] — [Kalshi Fee Schedule PDF](https://kalshi.com/docs/kalshi-fee-schedule.pdf); [marketmath](https://marketmath.io/platforms/kalshi); [pm.wiki](https://pm.wiki/learn/kalshi-fees-explained)
  - Caution: aggregator snippets contradict each other on per-contract rounding versus order-level rounding. One even claims a "$0.035 cap", which doesn't match the formula. Verify against the PDF.
- **Polymarket US:** fee = θ × C × p × (1−p). The US exchange fee schedule is effective April 3, 2026. The sports taker θ is 0.05 (max $1.25 per 100 contracts at 50¢), updated July 2026 from θ = 0.03 ($0.75 max). The maker rebate is −0.0125. [M] — [Polymarket US Fee Schedule](https://docs.polymarket.us/fees); [startpolymarket](https://startpolymarket.com/learn/polymarket-fees/)
- **Novig:**
  - One source says takers pay 0.03 × P × (1−P) (max about $0.0075/contract), makers pay nothing, and makers "can earn a credit". [M] — [Novig Help Center "Fees on Novig"](https://support.novig.com/en/articles/16195057-fees-on-novig); [oddsassist](https://oddsassist.com/prediction-markets/novig-fees/)
  - This is contradicted by CBS Sports (2026 NFL guide): "Novig doesn't charge any trading fees". [L-M] — [CBS Sports](https://www.cbssports.com/prediction/news/nfl-prediction-apps/)
  - The fee may have been introduced with the DCM relaunch (June–Aug 2026).
- **ProphetX:** 2% fee on net gains for straight trades. Parlays carry a trade fee charged to the taker only; makers pay no trade fee. [M-H, help center cited] — [ProphetX Help: Understanding Fees](https://prophethelp.zendesk.com/hc/en-us/articles/26975338569489-Understanding-Fees); [NEXTPredict](https://nextpredict.io/platforms/prophetx/)
- **Sporttrade:** 2% commission on profits, with no fee on losing bets. [M] — [oddsassist Sporttrade fees](https://oddsassist.com/sports-betting/sportsbooks/sporttrade-fees/); [BettingUSA](https://www.bettingusa.com/sports/reviews/sporttrade/)

### Inferences
- Taker cost at 50¢ (per $1 contract): Kalshi ≈1.75¢, Polymarket US ≈1.25¢, Novig ≈0.75¢ (if the fee applies).
- The profit-based 2% venues (ProphetX, Sporttrade) cost about 1¢ per winning contract bought at 50¢. Their cost structure favors high-probability sides less than P(1−P) fees do.
- Maker-heavy strategies are cheapest on Novig (0 fee) and Polymarket US (rebate).

### Gaps
- Whether Novig distinguishes pregame and live fees was not found.
- Whether Kalshi has sports-specific maker-fee series lists was not found.
- ProphetX's exact parlay taker rate was not found.

## 3. Liquidity and volume

### Takeaway
Kalshi dominates sports volume: $4.9B on NFL Week 1 weekend in September 2026, and a $15.27B total week. No published volume figures were found for Novig, ProphetX or Sporttrade.

### Cited Findings
- Kalshi did $4.9B in volume across NFL Week 1 weekend (September 2026). That beat its full prior-season NFL total of $7.21B. Daily records were $2.426B (Saturday) and $2.433B (Sunday). [M-H, multiple trade outlets] — [CDC Gaming](https://cdcgaming.com/brief/kalshi-posts-record-u-s-football-weekend-with-4-9b-volume/); [World Casino Directory](https://news.worldcasinodirectory.com/kalshi-breaks-trading-records-during-nfl-week-1-124351)
- The Kalshi week of Sept 14–20, 2026 totalled $15.27B, its 2026 high, and included its first $3B day. [M] — [DeFi Rate](https://defirate.com/news/kalshi-2026-volume-record-15b-week/); [SCCG](https://sccgmanagement.com/sccg-articles/2026/09/15/kalshi-sets-consecutive-daily-volume-records-during-2026-nfl-opening-week/)
- Parlays drove the back-to-back records to open football season. [M] — [Next Event Horizon](https://nexteventhorizon.substack.com/p/parlays-propel-kalshi-to-back-to-back-records)
- DeFi Rate projects 2026-27 prediction-market NFL volume at $56.51B in its conservative case and $128.14B in its high case. [L, projection] — [DeFi Rate](https://defirate.com/news/prediction-market-nfl-trading-volume-could-reach-57-billion/)
- ProphetX raised $35M in 2026 to scale its prediction market and B2B business. [M] — [Gaming Intelligence](https://www.gamingintelligence.com/finance/234222-prophetx-raises-35-million-to-expand-prediction-markets-offering/)
- Novig raised $18M in 2025. [M] — [Sportico](https://www.sportico.com/business/sports-betting/2025/novig-sweepstakes-legal-pressure-new-jersey-1234867485/)

### Inferences
- A large share of Kalshi's headline volume is parlays/combos, so single-game moneyline depth is lower than the totals suggest.
- Pregame depth on Novig and ProphetX is likely far thinner and depends on a few market makers. This is unverified.

### Gaps
- No source found for typical depth at best price on NFL/NBA/MLB game markets on any venue.
- No published Novig, ProphetX or Sporttrade volumes found.
- No Kalshi league-by-league breakdown beyond NFL found.

## 4. Account treatment of winning/automated traders

### Takeaway
All exchange venues are peer-to-peer and advertise no limits for winners. No specific reports of bans for bots were found. The practical constraint is liquidity, not limits.

### Cited Findings
- Novig is peer-to-peer, the house takes no position, and it "never has a reason to limit you". The practical limit is liquidity. [L-M, review site] — [XCLSV Novig review](https://xclsvmedia.com/novig-review-2026-peer-to-peer-sportsbook-sharp-bettors/)
- ProphetX says it has no in-house trading arm and relies on institutional market makers. Its API is explicitly "built for algorithmic traders and liquidity providers". [M] — [ProphetX APIs](https://docs.prophetx.co/)
- Kalshi publishes a public API with rate-limit tiers up to 8,000 tokens/s. That implies automated trading is sanctioned. — [Kalshi docs](https://docs.kalshi.com/getting_started/rate_limits)

### Inferences
- Bots are explicitly supported on Kalshi and ProphetX. On Novig, the fee page (per oddsassist) says API fees equal app fees, which implies API trading is permitted.

### Gaps
- No Reddit or user reports were retrieved on account restrictions, KYC issues or withdrawal delays for any venue; reddit was not searched successfully.

## 5. Legal/regulatory status in New York

### Takeaway
New York is the most hostile state for these venues, and access to every one of them is under active legal threat.
- **Kalshi:** NY sued it on July 31, 2026 for $36B+. Kalshi lost its federal preliminary-injunction bid (SDNY, Judge Torres, July 2026) and has an appeal pending at the Second Circuit.
- **Polymarket US:** NY sued it on Sept 24, 2026.
- **Coinbase and Gemini:** NY sued both around April 2026.
- **Novig:** filed a pre-emptive federal suit against NY on Aug 5, 2026.
- **CFTC:** sued NY on April 24, 2026.
- **Sporttrade:** not available in NY.

These venues are reportedly still accessible to NY residents as of September 2026, but shutdown risk is high.

### Cited Findings
- **NY v. Kalshi:** NY AG Letitia James and Gov. Hochul sued Kalshi on July 31, 2026, seeking at least $36B. They allege an unlicensed gambling operation that lets 18+ New Yorkers bet on sports. The state wants:
  - an order to stop operating
  - disgorgement (claimed nationwide), restitution, and 3× gains plus $100K civil penalty per illegal bet offer.

  Kalshi called it "political theater". NY is at least the 14th state to move against Kalshi or a similar platform. [H] — [NY AG press release](https://ag.ny.gov/press-release/2026/governor-hochul-and-attorney-general-james-announce-new-york-has-sued-kalshi); [CNBC](https://www.cnbc.com/2026/07/31/new-york-sues-kalshi-claims-it-is-illegal-gambling-operation.html); [Petition PDF](https://ag.ny.gov/sites/default/files/court-filings/new-york-v-kalshiex-llc-petition-2026.pdf)
- **Kalshi v. NY Gaming Commission (SDNY):**
  - Judge Analisa Torres denied Kalshi's TRO and preliminary injunction around July 8, 2026. She held that the CEA does not preempt NY gambling law as applied to sports event contracts, and that geolocation and federal registration don't exempt Kalshi from licensing. [H]
  - Kalshi appealed to the Second Circuit the same day. [H]
  - In late July 2026, a single Second Circuit judge declined to rule alone on an injunction pending appeal and referred it to a three-judge panel. [M] — [Law360](https://www.law360.com/articles/2507905/2nd-circ-judge-denies-kalshi-shield-from-ny-action-for-now)

  Sources: [Covers](https://www.covers.com/industry/new-york-judge-denies-kalshis-bid-to-block-state-gambling-enforcement-july-8-2026); [Yogonet](https://www.yogonet.com/international/news/2026/07/08/125283-kalshi-appeals-after-federal-judge-rejects-bid-to-block-new-york-gambling-enforcement); [Gaming Today](https://www.gamingtoday.com/news/new-york-federal-court-denies-kalshi-injunction/)
- Kalshi is still accessible in New York as of September 2026, though contested. [M] — [Covers "Is Kalshi legal in NY? Sept 2026"](https://www.covers.com/betting/prediction-sites/usa/new-york-kalshi)
- A Supreme Court cert decision on Kalshi-related state cases (including a New Jersey petition) could come at the Sept 28, 2026 long conference. [M] — [Startup Fortune](https://startupfortune.com/new-jersey-asks-supreme-court-to-decide-if-kalshis-sports-betting-is-legal/); [straighttothepoint](https://straighttothepoint.substack.com/p/kalshis-legal-headaches-keep-multiplying)
- **NY v. Polymarket:** NY AG and Governor sued Polymarket's US business on Sept 24, 2026 in Manhattan state court. They seek to block operation without a license, plus restitution, forfeiture and penalties, and they object to letting 18-20-year-olds trade (NY's mobile sports betting age is 21). [H] — [CoinDesk](https://www.coindesk.com/policy/2026/09/24/new-york-sues-polymarket-alleging-it-is-running-an-illegal-gambling-operation); [Al Jazeera](https://www.aljazeera.com/economy/2026/9/24/new-york-sues-polymarket-over-allegations-of-illegal-gambling-operations)
- NY had earlier (about 5 months before, i.e. around April 2026) filed petitions against Coinbase Financial Markets and Gemini Titan. [H] — [CoinDesk](https://www.coindesk.com/policy/2026/09/24/new-york-sues-polymarket-alleging-it-is-running-an-illegal-gambling-operation)
- **CFTC v. New York:** on April 24, 2026 the CFTC (with DOJ) sued NY in SDNY. It seeks a declaratory judgment of exclusive federal authority over event contracts and a permanent injunction against state enforcement against its registrants. It had filed similar suits against Arizona, Connecticut and Illinois on April 2, 2026. [H, primary] — [CFTC press release 9218-26](https://www.cftc.gov/PressRoom/PressReleases/9218-26); [US News/Reuters](https://money.usnews.com/investing/news/articles/2026-04-24/cftc-sues-new-york-to-block-oversight-of-prediction-markets)
- **Polymarket US status:**
  - The US exchange launched Dec 2, 2025 with CFTC approval, invite-only at first. The waitlist was removed in May 2026 (iOS/Android, web in beta). [M-H] — [SBC Americas](https://sbcamericas.com/2026/05/13/polymarket-us-exchange-nationwide-reach/); [Sportico](https://www.sportico.com/business/sports-betting/2026/polymarket-united-states-launch-invite-waitlist-delay-1234879944/)
  - It is live in NY and 40+ states as of September 2026, before the lawsuit. [M] — [TheLines](https://www.thelines.com/prediction-markets/polymarket/new-york/)
- **Novig:**
  - It became a CFTC Designated Contract Market on June 16, 2026 and launched in 47 states. [M-H]
  - On Aug 5, 2026 it sued NY in federal court, seeking a preliminary injunction against the AG and Gaming Commission members. It cited the NY action against Kalshi as an imminent threat. [M-H]
  - It was earlier a sweepstakes operator and exited New Jersey under legal pressure in 2025. NY separately banned sweepstakes sportsbooks. [M]
  - Some states (e.g. Arizona) have issued cease-and-desist orders. [M]

  Sources: [Prediction News](https://predictionnews.com/story/novig-sues-new-york-in-federal-court-day-after-launching-regulated-prediction-ma); [SBC Americas](https://sbcamericas.com/2026/08/10/prediction-market-suit-novig-new-york/); [ReadWrite](https://readwrite.com/novig-sues-new-york-federal-law/); [Sportico 2025](https://www.sportico.com/business/sports-betting/2025/novig-sweepstakes-legal-pressure-new-jersey-1234867485/); [SportsBettingDime](https://www.sportsbettingdime.com/prediction-markets/novig/legal-states/)
- **ProphetX:**
  - The CFTC approved it as a DCM and DCO on June 12, 2026. It is New York-based and launched nationwide on June 18, 2026. [M-H] — [Covers](https://www.covers.com/industry/prophetx-prediction-market-license-approved-cftc-sports-betting-june-2026); [PR Newswire launch](https://www.prnewswire.com/news-releases/prophetx-launches-nationwide-302804384.html)
  - Whether "nationwide" explicitly includes NY could not be confirmed from the release text.
- **Sporttrade:** a state-licensed sportsbook exchange in AZ, CO, IA (exchange not available), NJ and VA. It is not available in NY. [M] — [BettingUSA](https://www.bettingusa.com/sports/reviews/sporttrade/)

### Inferences
- A NY trader can currently reach Kalshi, Polymarket US, Novig and (likely) ProphetX. Sporttrade is out.
- Kalshi's loss at the district court and the pending Second Circuit ruling mean a NY geofence or injunction could arrive on short notice. Bots need kill-switches and a plan to unwind positions.
- The Polymarket suit was filed on the research date; expect further changes to its NY access.

### Gaps
- Whether any venue has already geofenced NY in response to a court order was not confirmed.
- No ProphetX- or Novig-specific NY cease-and-desist was found.
- The outcome of the Second Circuit panel on an injunction pending appeal was not found.

## 6. Settlement rule differences (ties, OT, postponements)

### Takeaway
- **Postponements:** Kalshi game markets use a 48-hour window, and a longer postponement or cancellation settles at a "fair market price" rather than voiding.
- **Ties:** tied games in markets that allow them settle at 50¢ each.
- **Novig:** no comparison could be sourced.

### Cited Findings
- Kalshi postponed-game rules involve a 48-hour clock, an NFL "55-minute clause" and a shootout provision that determine whether a market voids. Cancelled or long-postponed games settle to a fair market price. [M, OddsShopper summarizing Kalshi rules] — [OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-postponed-game-rules)
- When a game ends tied after overtime, each team's contract resolves at 50¢. [M] — [search summary; CBS/OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/how-to-bet-nfl-on-kalshi)
- Each Kalshi market's terms name an official data source and resolution date before trading opens. Stat corrections have led to disputed settlements (the "NFL 9-win case"). [M] — [Kalshi Help Market FAQs](https://help.kalshi.com/en/articles/13823821-market-faqs); [OddsShopper stat corrections](https://www.oddsshopper.com/articles/prediction-markets/kalshi-stat-corrections-settlement)

### Inferences
- Cross-venue arb between Kalshi and a venue that voids postponed games (common sportsbook convention) carries basis risk on postponements and ties.

### Gaps
- Novig, ProphetX and Polymarket US settlement rules for ties, OT and postponements were not found.
- Kalshi per-league (NBA/MLB/NHL) specifics were not verified from rulebooks.
