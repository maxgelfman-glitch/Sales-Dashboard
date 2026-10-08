# Exchange Fees, Maker Incentives, Liquidity and Microstructure: Sports Event-Contract Venues for a NY Resident (as of 2026-10-08)

Method note: researched 2026-10-08. Direct page fetches (kalshi.com, help.kalshi.com, cftc.gov, docs.novig.com, theblock.co) were blocked by the sandbox network policy. All findings below come from search-engine extracts of the named pages, so exact wording and numbers should be re-checked against the primary page before capital is committed. "OFFICIAL" means the extract came from a Kalshi, Novig or CFTC page. "3P" means a third-party or affiliate review site, many of which earn affiliate fees.

## 1. Kalshi: fees, liquidity/volume incentive programs, API limits, volumes, spreads and depth

### Takeaway
- **Fees:** the Kalshi taker fee is ceil(0.07 × C × P × (1−P)), at most $1.75 per 100 contracts at 50¢. Maker fees, where charged, are 0.0175 × C × P × (1−P), a quarter of the taker rate. Whether ordinary sports game markets carry a maker fee in 2026 is disputed between sources. Combo maker fees are officially 50% of the taker fee.
- **Liquidity Incentive Program (LIP):** pays $1 to $1,000 per market per day. Payouts are scored from random per-second order-book snapshots, weighted by size and distance from a reference price. Since February 2026 it pays only for two-sided books, and it runs to 2027-01-01.
- **Volume Incentive Program:** ends no earlier than 2026-10-13, after wash-trading scrutiny.
- **Volume:** Kalshi dominates, with roughly 78% to 86% of tracked venue volume.
- **NFL pricing:** Week 1 2026 implied vig was 4.32%, below FanDuel (4.44%) and DraftKings (4.51%).

### Cited Findings
**Fees**
- (3P, consistent across several guides) The taker fee is ceil(0.07 × C × P × (1−P)) per order, rounded up to the cent. The maker fee is round-up(M × 0.0175 × C × P × (1−P)), charged only when the resting order executes. Cancelling is free. Examples: at 10¢, taker 0.63¢ and maker 0.16¢; at 50¢, taker 1.75¢ and maker 0.44¢ per contract. — [MarketMath](https://marketmath.io/blog/kalshi-fees-guide-2026); [pm.wiki](https://pm.wiki/uk/learn/kalshi-fees-explained); [rivermarkets](https://www.rivermarkets.com/insights/kalshi-fees.html)
- (OFFICIAL, via search extract of kalshi.com/fee-schedule, page undated)
  - Most markets have fee multiplier 1, and taker fees range from $0.07 to $1.75 per 100 contracts.
  - For combos, the maker fee is 50% of the taker fee, except uncorrelated NFL combos.
  - No upcoming fee changes were listed.
  — [Kalshi Fee Schedule](https://kalshi.com/fee-schedule)
- (3P, CONFLICT) Sources disagree on sports maker fees:
  - One says resting orders are fee-free on most sports markets.
  - Others say maker fees apply on many markets.
  - Another claims a "flat 0.25% maker fee" during major events such as NFL championships and NBA Finals. This could not be matched to the official schedule.
  - One source counts maker fees on "156 non-standard series".
  - Fees "have changed repeatedly through 2026".
  — [Stokastic](https://www.stokastic.com/articles/prediction-markets/kalshi-fees-vs-dfs-rake); [prediction.com](https://prediction.com/blog/kalshi-fees-complete-guide-2026); [predictreport](https://predictreport.io/blog/kalshi-fees-explained); [pm.wiki](https://pm.wiki/uk/learn/kalshi-fees-explained)
- (3P) Citizens JMP measured an average Kalshi transaction fee of $1.62 per 100 contracts on NFL Week 1 2026. — [NextPredict](https://nextpredict.io/market-news/industry/kalshi-nfl-week-1-volume-733m/)
- (3P estimate) More than 89% of Kalshi's estimated $263.5M of 2025 fee revenue came from sports. — [RevenueMemo](https://www.revenuememo.com/p/how-does-kalshi-make-money)
- (OFFICIAL CFTC, June 2026) Kalshi's perpetual futures have a separate tiered fee schedule, with maker fees from 5.0 bps down to 0.6 bps. This does not apply to sports event contracts. — [CFTC filing](https://www.cftc.gov/filings/orgrules/rules0608265590.pdf)

**Liquidity Incentive Program (LIP)**
- (OFFICIAL help center) The program rewards resting orders that improve liquidity. Kalshi records random snapshots during trading hours and scores orders by size and proximity to the best price.
  - The pool for each market is shown on the market page under "Rewards".
  - Most regular US members are eligible.
  - The help page lists a period of 2025-09-15 to 2026-09-01, and Kalshi can modify or end the program at any time.
  - A 3P guide says the end date was extended to 2027-01-01 under the CFTC update of 2026-07-15.
  — [Kalshi Help: LIP](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program); [CFTC LIP update 2026-07-15](https://www.cftc.gov/filings/orgrules/rules07152610358.pdf); [Kalshi Help: where to find LIPs](https://help.kalshi.com/en/articles/16076644-liquidity-incentive-programs-where-to-find-them)
- (OFFICIAL CFTC terms, via extract) **Scoring mechanics:**
  - **Snapshots:** taken once per second at a uniformly random moment.
  - **Reference Price:** walk down from the best bid to the first level where cumulative size reaches 1/5 of the Target Size.
  - **Per-order score:** Discount Factor^N × size, where N is the number of ticks from the Reference Price. The YES and NO books are scored separately, and a YES ask counts as a NO bid.
  - **Parameter bounds:** Target Size is between 100 and 20,000 contracts. The Discount Factor is at most 1.00.
  - **Reward per market:** at least $1 (amended down from $10) and at most $1,000 per calendar day. Payouts are rounded down to the cent, and balances under $1 are not paid.
  — [CFTC LIP update 2026-07-15](https://www.cftc.gov/filings/orgrules/rules07152610358.pdf); [Kalshi Help](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program)
- (OFFICIAL CFTC, amendment filed 2026-02-11, effective 2026-02-28) Snapshots with no qualifying YES bids or no qualifying NO bids are excluded, so rewards now go only to two-sided markets. — [CFTC filing](https://www.cftc.gov/sites/default/files/filings/orgrules/26/02/rules02112639183.pdf)
- (OFFICIAL help center) **Sports Prop Combo Market Component Legs LIP:**
  - **Pool:** 25% of the fees from Sports Prop Combo markets (the "Component Liquidity Incentive Fee Fraction"). Example: $100 of fees gives a $25 pool.
  - **Allocation:** the pool is split by maker volume in eligible component legs. Only props qualify, not game winners, spreads or totals.
  - **Live games:** only maker volume traded after the scheduled start counts.
  - **Partial eligibility:** the payout is scaled by the fraction of eligible legs, and the scoring rewards breadth across legs.
  - **Eligibility:** all members except Kalshi affiliates and IB/FCM customers. There is no opt-in.
  - **Payment:** minimum individual score 0.01, paid monthly as trading credits, with the program ending 2027-01-01.
  — [Kalshi Help: Prop Combo LIP](https://help.kalshi.com/en/articles/17184676-sports-prop-combo-market-component-legs-liquidity-incentive-program); [NextPredict](https://nextpredict.io/market-news/industry/kalshi-files-prop-market-liquidity-incentives/)

**Other incentive programs**
- (OFFICIAL CFTC) **Sportsbook Hedging Rebate Program**, starting on or after 2026-02-23 and running to 2027-02-01:
  - All taker fees and RFQ fees are rebated for orders over 300,000 contracts traded for sportsbook hedging.
  - It is open to entities offering sportsbook services, which must attest to qualification and are subject to audit.
  - Reports conflict on whether the threshold is per order or per month.
  — [CFTC filing](https://www.cftc.gov/sites/default/files/filings/orgrules/26/02/rules02072638946.pdf); [InGame](https://www.ingame.com/kalshi-fee-rebates-sportsbooks/); [GamingAmerica](https://gamingamerica.com/news/1006858/kalshi-launches-prediction-market-rebate-program-for-sports-event-contracts)
- (OFFICIAL notice reported by press) **Volume Incentive Program:**
  - Kalshi's 2026-09-28 notice to the CFTC terminates the program no earlier than 2026-10-13. It had been scheduled to run to 2027-10-01.
  - It paid by share of volume, capped at $0.005 per contract for event contracts, counting only trades priced between 3¢ and 97¢.
  - Press ties the termination to wash-trading scrutiny of repetitive fixed-size trades of about $5,500 in ETH perpetuals.
  - Kalshi denies wash trading. It says self-match prevention is in place and attributes the pattern to fixed-size market-maker quotes being hit repeatedly.
  — [LSR](https://www.legalsportsreport.com/279648/volume-rewards-program-on-kalshi-set-to-end-nearly-a-year-early/); [SBC Americas](https://sbcamericas.com/2026/10/02/kalshi-end-volume-rewards-scrutiny/); [DeFi Rate](https://defirate.com/news/kalshi-volume-rewards-cftc-scrutinizes-prediction-markets/); [Gambling.com](https://www.gambling.com/us/news/kalshi-end-volume-incentive-program-october-13); [The Block](https://theblock.co/news/business/2026-09-30-kalshi-ends-trader-incentive-program-417247)
- (OFFICIAL CFTC, reported) **Deposit and Trading Reward Program**, implemented on or after 2026-09-28:
  - Time-limited promotions tied to deposits and/or trading.
  - Up to $2,500 per participant per promotion and $5,000 per person over the program's two-year life.
  - Trades under wash-trading inquiry are excluded.
  — [CFTC filing](https://www.cftc.gov/filings/orgrules/rules09252630037.pdf); [DeFi Rate](https://defirate.com/news/kalshi-volume-rewards-cftc-scrutinizes-prediction-markets/)

**API rate limits**
- (OFFICIAL docs plus 3P) Kalshi moved to token-bucket rate limits, which community guides date to April 2026.
  - Most requests cost 10 tokens. Read and write buckets are separate.
  - 3P tier figures (tokens per second):

    | Tier | Read | Write | Approx. orders/sec |
    |---|---|---|---|
    | Basic | 200 | 100 | 10 |
    | Advanced | 300 | 300 | 30 |
    | Premier | n/a | 1,000 | 100 |
    | Prime | 4,000 | n/a | n/a |

    ("n/a" means the tier was not split into read and write figures in the source.)
  - The official doc example instead shows a Premier write bucket refilling at 1,200 tokens per second with a cap of 3,600, about 120 orders per second.
  - Batch orders cost N × 10 tokens.
  - Advanced is a self-serve upgrade. Premier is reached automatically through trailing 30-day volume (3P).
  - Check the live figures with GET /account/limits.
  — [Kalshi API docs: Rate Limits](https://docs.kalshi.com/getting_started/rate_limits); [Parlay.run guide](https://www.parlay.run/kalshi-api); [Kairos](https://kairos.trade/compare/kalshi-api)

**Volume**
- (3P tracker) Week of 2026-09-28 to 10-04:
  - Kalshi total volume $18.37B, up 17.3% week on week. Sports-classified series were $3.64B of that. The total likely includes the new perpetuals.
  - Polymarket US: $2.71B.
  - Polymarket global, by an Eastern-calendar dollar measure: $519M.
  — [DeFi Rate weekly report](https://defirate.com/news/sept-28-oct-4-2026-volume-report/)
- (3P tracker) Share of tracked volume, 2026-09-07 to 10-06: Kalshi 78%, Polymarket US 12%, Polymarket 5%, Novig 2%. — [DeFi Rate Kalshi volume](https://defirate.com/prediction-markets/volume/kalshi/). Bitrue puts Kalshi at 84% to 86% in early September 2026. — [Bitrue](https://www.bitrue.com/blog/kalshi-vs-polymarket)
- (Pew Research, 2026-09-23) Sports trading in June and July 2026, the World Cup period, exceeded $58B on Kalshi and neared $22B on Polymarket. Total prediction-market volume doubled from May to July. — [Pew](https://www.pewresearch.org/short-reads/2026/09/23/prediction-markets-trading-volume-doubled-between-may-and-july-largely-driven-by-sports/)
- (3P) NFL volume:
  - Kalshi did $733.1M of non-combo NFL volume in Week 1 2026, up 174% year on year.
  - Another outlet reports a record $4.9B football weekend, which likely counts all football and combos.
  - An NFL season forecast of about $57B has been reported.
  — [NextPredict](https://nextpredict.io/market-news/industry/kalshi-nfl-week-1-volume-733m/); [Prediction News](https://predictionnews.com/story/kalshi-posts-record-4-9-billion-football-weekend-volume); [Prediction News](https://predictionnews.com/story/kalshi-nfl-trading-volume-projected-at-57-billion-for-2026-27-season)
- (NY AG release, cited) In 2025, Kalshi users traded over $1B a month, with 90% of it on sports. — [Covers](https://www.covers.com/industry/kalshi-new-york-lawsuit-allege-illegal-sports-betting-billions-fines-july-2026)

**Spreads, depth and pricing efficiency**
- (Citizens JMP analysts, via press, sampled 2026-09-11 across 28 moneyline and total prices)
  - Kalshi's implied vig was 4.32% before transaction fees, against FanDuel 4.44% and DraftKings 4.51%.
  - Over the 2025 NFL season, Kalshi's vig was 4.84%, against FanDuel 4.42% and DraftKings 4.48%.
  - Combos (favorite plus over) were worse on Kalshi: 23.8% against 22.0% at the sportsbooks.
  - Citizens attributes the improvement to more liquidity-provider competition.
  — [Covers](https://www.covers.com/industry/kalshi-edges-fanduel-draftkings-sportsbooks-in-nfl-week-1-pricing-sept-16-2026); [NextPredict](https://nextpredict.io/market-news/industry/kalshi-nfl-week-1-volume-733m/); [Next.io](https://next.io/news/betting/kalshi-undercuts-draftkings-and-fanduel-in-nfl/)
- (3P, three-game NFL Week 1 sample) Kalshi's books were 1¢ wide, for example 35/36 on the 49ers and 33/34 on the Commanders. Polymarket's were 2¢ to 3¢ wide, for example 35/37 and 31/34. — [OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-vs-polymarket-nfl)
- (3P anecdote, August 2026) Over 20,000 contracts rested at the 67¢ ask on an Eagles market. It is unclear whether this was a game or futures market. — [OddsShopper/SI search extract](https://www.oddsshopper.com/articles/prediction-markets/how-to-trade-nfl-on-kalshi)
- (3P) FanDuel market-makes directly on Kalshi NFL contracts. That concentrates liquidity in a single provider. — [Bodog analysis](https://bodog.com/prediction-market/kalshis-nfl-volume-is-exploding-should-sportsbooks-be-worried)

### Inferences
- **Pregame makers on Kalshi:** at a mid price with a 1¢ spread, a taker pays about 1.75¢ per contract. A maker earns half the spread (about 0.5¢) and may pay 0.44¢ if a maker fee applies. Before LIP, net maker edge is therefore close to zero in liquid NFL and NBA games. LIP rewards (up to $1,000 per market per day), shared among competing quoters, are likely a major part of pregame market-making P&L.
- **Two-sided quoting:** the February 2026 rule pays only when both sides are quoted. One-sided "lean" quoting therefore earns nothing.
- **Combo quoting:** combo makers pay 50% of the taker fee, but prop-leg makers share 25% of combo fees. Combo quoting is best run as a combined book of prop legs plus combos, and the prop-leg LIP can offset maker fees.
- **Rebate-farming strategies:** these face regulatory headwinds. The volume program's end and the scrutiny around it suggest share-of-volume rewards are going away. Depth-based LIP remains.

### Gaps
- The official 2026 maker-fee flag for standard NFL, NBA and MLB game series could not be verified. It needs a pull of the `fee_type` and multiplier fields from the series API.
- Actual LIP pool sizes per sports market in 2026 were not found; they are visible only in the in-app Rewards popover.
- No systematic top-of-book depth statistics for NBA or MLB were found.
- No information was found on Kalshi designated market maker agreements, beyond FanDuel acting as a market maker. Susquehanna and similar firms were not confirmed in 2026 sources.

## 2. Novig: fees, Maker Credit, LP programs, volume, NY availability

### Takeaway
- **Fees:** the taker fee is 0.03 × P × (1−P), at most about 0.75¢ per contract. It is charged only on live (in-game) trades and on futures. Pregame trading and maker fills are free.
- **Maker Credit:** 50% of the live taker fee collected, or 70% for eligible futures. Pregame fills earn no credit.
- **Exchange status:** Novig's exchange entity, Ludlow Exchange, received CFTC designated contract market (DCM) status on 2026-06-16.
- **Volume:** about $41M per day on average, per Novig's own data site.
- **New York:** Novig is listed as restricted in NY and is suing the state. Treat it as unavailable to NY residents unless that changes.

### Cited Findings
**Fees and Maker Credit**
- (3P, consistent with official docs) The taker fee is 0.03 × P × (1−P), capped near $0.0075 per contract at 50¢. It applies to taker trades on live, in-game markets. Pregame trades and maker fills pay nothing. — [OddsAssist Novig fees](https://oddsassist.com/prediction-markets/novig-fees/); [Novig API: Trading Fees](https://docs.novig.com/fees)
- (3P, unconfirmed) Parlays carry 0.10 × P × (1−P), built into the quote. — [OddsAssist](https://oddsassist.com/prediction-markets/novig-fees/)
- (OFFICIAL support) **Maker Credit Program:**
  - The credit is 50% of the Live Taker Fee collected on the trade, or 70% of the Futures Taker Fee for eligible futures markets.
  - Fees assessed at any other time, including pregame, generate no credits.
  - The credit shrinks if the counterparty's fee was reduced, waived or refunded.
  - All members are eligible except exchange affiliates and members with a Market Maker Agreement.
  - There is no opt-in. One 3P source says credits are paid within 7 days.
  — [Novig Support: Maker Credit Program](https://support.novig.com/en/articles/16116780-maker-credit-program); [Novig API: Trading Fees](https://docs.novig.com/fees)
- (OFFICIAL docs, via extract) Liquidity providers with $200,000 or more deposited can request a dedicated Slack channel with Novig. The fee contact listed is caleb.henry@novig.co. Note: this $200k threshold conflicts with the "$30k LP onboarding" figure in the brief; no source for $30k was found. — [Novig API: Trading Fees](https://docs.novig.com/fees)
- (3P reporting on CFTC filings) Ludlow filed six incentive programs before launch. They include a Liquidity Provider Program with reward pools of up to $50,000 per eligible market, based on how competitive and how persistent resting orders are. Which programs are active was not disclosed. — [DeFi Rate](https://defirate.com/news/novig-launches-federally-regulated-sports-prediction-market-platform-through-ludlow-exchange/)
- (OFFICIAL docs) **API:**
  - Subaccounts each have their own balance and trading key.
  - A management key funds subaccounts but cannot trade.
  - Requests are signed with Ed25519 or P-256.
  - Onboarding requires accepting the Bitnomial Clearinghouse acknowledgment and the Ludlow rulebook.
  — [Novig API Overview](https://docs.novig.com/); [DeFi Rate](https://defirate.com/news/novig-launches-federally-regulated-sports-prediction-market-platform-through-ludlow-exchange/)

**Volume**
- (OFFICIAL data site, snapshot 2026-10-07)
  - About $2.66B of volume since 2026-08-04, averaging about $41M per day.
  - Record day: $158M on 2026-10-04.
  - Daily trades.csv (one row per side) and markets.csv (open interest, volume, prices) are free to download.
  — [data.novig.com](https://data.novig.com/)
- (3P) DeFi Rate reports $1.4B over the last 30 days and $2.0B year to date 2026. It uses a different method from Novig's own site. — [DeFi Rate Novig volume](https://defirate.com/prediction-markets/volume/novig/)

**Regulatory status and New York**
- (Press) The CFTC granted DCM status to Ludlow Exchange LLC, Novig's exchange entity, on 2026-06-16. Novig raised a $75M Series B in March 2026. — [CNBC](https://www.cnbc.com/2026/06/16/novig-wins-cftc-approval-as-competition-intensifies-in-sports-prediction-markets.html); [SBC Americas](https://sbcamericas.com/2026/06/17/novig-prediction-market-cftc-approval/); [PRNewswire](https://www.prnewswire.com/news-releases/novig-raises-75m-series-b-to-build-a-trader-first-sports-prediction-market-302691216.html)
- (Press) Novig launched its prediction markets in 47 states. One day later it sued New York, seeking a preliminary injunction against the NY Attorney General and Gaming Commission. — [Yahoo Finance](https://finance.yahoo.com/markets/options/articles/novig-sues-york-1-day-214000200.html)
- (3P review, about late September 2026) Restricted states: AL, AZ, CA, CO, CT, ID, LA, MI, MT, NJ, NV, NY, TN, WA. — [casino.org / review extract](https://www.casino.org/us/predictions/novig/)

### Inferences
- For a NY resident, Novig is probably not legally accessible as of 2026-10-08. Any cross-venue strategy using Novig would require residency or access in another state.
- **Pregame economics:** with zero fees for takers and makers, pregame on Novig is the cheapest venue to cross. Makers earn no credit pregame, so pregame making relies on spread alone, plus any active LP pool.
- **Live economics:** in-game, a maker earns 50% of 0.03 × P × (1−P), about 0.375¢ at 50¢. Kalshi's taker fee is higher, so Novig in-game liquidity can be used to hedge Kalshi more cheaply.

### Gaps
- No pregame depth or spread statistics were found for Novig.
- Which Ludlow incentive programs are active, and their actual pool sizes, were not found.
- No source was found for the $30k LP onboarding figure.
- No ruling was found in the Novig v. New York case.

## 3. ProphetX: fees, maker programs, NY availability, API

### Takeaway
- **Fees (3P):** 2% of net winnings on straight trades, 1.5% for VIP, and 0% on parlays. Losers and unmatched orders pay nothing.
- **Regulation:** the CFTC approved ProphetX as both a DCM and a derivatives clearing organization (DCO) on 2026-06-11, and it launched nationwide on 2026-06-18.
- **Unknowns:** no market maker program terms were found, and NY-specific availability is not documented.

### Cited Findings
- (3P) ProphetX charges 2% of profit on straight trades and nothing on losing or cancelled trades. Parlays are 0%. A VIP tier is 1.5%. The App Store listing still references an older 3% fee from the sweepstakes era. — [OddsAssist](https://oddsassist.com/prediction-markets/prophetx-fees/); [PredictionScout](https://predictionscout.com/reviews/prophetx-review/); [Kairos](https://kairos.trade/compare/prophetx-review); [CoinStats](https://coinstats.app/prediction-markets/apps/prophetx/)
- (Press) ProphetX's CFTC DCM and DCO approvals were announced 2026-06-11. The nationwide launch on iOS and Android was 2026-06-18. ProphetX is headquartered in New York. — [Yahoo/PR](https://finance.yahoo.com/markets/options/articles/prophetx-obtains-cftc-approval-operate-181000686.html); [PRNewswire](https://www.prnewswire.com/news-releases/prophetx-launches-nationwide-302804384.html); [Covers](https://www.covers.com/industry/prophetx-prediction-market-license-approved-cftc-sports-betting-june-2026)
- (3P) ProphetX is not operating in Nevada, and Connecticut ordered it to stop sports contracts. — [CoinStats](https://coinstats.app/prediction-markets/apps/prophetx/)

### Inferences
- **Fee cost by price:** a 2% fee on winnings is cheapest relative to a P × (1−P) fee at low prices. At 50¢, a $1 contract wins 50¢ net, so the fee is 1¢ when the contract wins. In expectation that is about 0.5¢ per contract, roughly a third of Kalshi's 1.75¢ taker fee. A fee on winnings also falls on makers, which hurts market-making economics.
- **Combo arbitrage:** 0% parlay fees make ProphetX a potential cheap venue for combo-versus-legs arbitrage.

### Gaps
- No ProphetX market maker program or rebate terms were found.
- No API documentation or rate limits were found.
- NY availability is unknown, and the NY AG has sued several venues.
- No volume figures for ProphetX were found.

## 4. Cross-venue: Polymarket US, Robinhood, Crypto.com, price gaps and arbitrage frequency

### Takeaway
- **Polymarket US:** listed as available in every state except Nevada, but sued by NY in late September 2026. Its sports taker coefficient is about 0.05 (at most $1.25 per 100 contracts). Makers pay nothing and earn a rebate of 15% to 25% of the taker fee, a point on which sources conflict.
- **Robinhood:** routes sports contracts through Kalshi and ForecastEx.
- **Exchange-to-exchange gaps:** small in NFL markets, about 1¢ to 3¢.
- **Arbitrage:** most arbitrage involving an exchange has a sportsbook on the other side.

### Cited Findings
- (3P, conflicting) **Polymarket US fees:**
  - The sports taker coefficient rose from 0.03 to 0.05 in July 2026, so the maximum is $1.25 per 100 shares.
  - The sports maker rebate share fell from 25% to 15%.
  - Another source gives a uniform 0.06 theta (at most $1.50 per 100) and a 0.0125 maker rebate coefficient.
  - A volume-tiered taker rebate of 3% to 50% across 7 tiers was reported in May 2026 but not confirmed officially.
  — [StartPolymarket](https://startpolymarket.com/learn/polymarket-fees/); [PredictionHunt](https://www.predictionhunt.com/blog/polymarket-fees-complete-guide); [MarketMath](https://marketmath.io/news/polymarket-taker-rebate-program-2026); [Kairos](https://kairos.trade/compare/polymarket-fees)
- (Press) The NY AG and Governor sued Polymarket in late September 2026. Polymarket allows users aged 18 and up, while NY mobile sports betting requires 21. — [Spectrum News](https://spectrumlocalnews.com/nys/central-ny/news/2026/09/28/new-york--polymarket-clash-over-who-gets-to-regulate-prediction-markets)
- (3P) Polymarket US launched 2025-12-03 and is listed as available in all states and DC except Nevada. — [DeFi Rate list](https://defirate.com/prediction-markets/)
- (Press) The Yankees named Polymarket their official prediction market partner. — [SportsHandle](https://sportshandle.com/yankees-polymarket-prediction-market-partner/)
- (Press) Robinhood offers sports event contracts through Kalshi and ForecastEx. The NY AG has also acted against Coinbase and Gemini prediction offerings. — [NerdWallet](https://www.nerdwallet.com/investing/learn/what-are-prediction-markets); [Spectrum News](https://spectrumlocalnews.com/nys/central-ny/news/2026/09/28/new-york--polymarket-clash-over-who-gets-to-regulate-prediction-markets)
- (Press) **Kalshi in New York:**
  - The NY AG sued Kalshi on 2026-07-31, seeking an injunction, disgorgement and penalties of at least $36B. The AG's test trades from NY accounts went through.
  - The CFTC sued NY the same day, arguing federal preemption.
  - On 2026-07-08 an SDNY judge denied Kalshi's request to block NY regulators, finding no preemption.
  - Kalshi has injunctions against Nevada and New Jersey; the New Jersey one was affirmed by the Third Circuit.
  — [CNBC](https://www.cnbc.com/2026/07/31/new-york-sues-kalshi-claims-it-is-illegal-gambling-operation.html); [Forbes](https://www.forbes.com/sites/zennonkapron/2026/08/04/new-york-wants-36-billion-from-kalshi-a-federal-judge-next-door-just-shielded-it/); [Covers](https://www.covers.com/industry/kalshi-new-york-lawsuit-allege-illegal-sports-betting-billions-fines-july-2026)
- (3P) **NFL price gaps:**
  - NFL MVP futures on 2026-09-15: Josh Allen at 17¢ on Kalshi against 18¢ on Polymarket.
  - Drake Maye in late August: 6¢ to 7¢ on Kalshi against 9¢ to 10¢ on Polymarket US.
  - Including fees, the best sportsbook price beat Kalshi on 23 of 28 NFL prices. Against FanDuel alone, Kalshi was about even (14 wins, 13 losses, 1 tie).
  — [LaikaLabs](https://laikalabs.ai/prediction-markets/nfl-mvp-odds-kalshi-vs-polymarket); [OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-vs-sportsbooks-nfl)
- (3P, esports, 2026-09-06 to 10-06) About 15.9% of arbitrage opportunities had a Polymarket or Kalshi leg. Arbitrage between the two exchanges was rare, and the other leg was almost always a sportsbook. Exchanges were relatively stronger pre-match. — [DEV Community](https://dev.to/dozor/polymarket-kalshi-arbitrage-vs-sportsbooks-30-days-of-esports-data-1dc2)

### Inferences
- **Round-trip cost of an exchange-to-exchange arbitrage at mid prices:** Kalshi taker (about 1.75¢) plus Polymarket US taker (about 1.25¢), so roughly 3¢. With observed gaps of 1¢ to 3¢ on liquid NFL, taker-taker arbitrage is rarely profitable.
- **Profitable structures:**
  - Maker on one venue and hedge as taker on the cheaper venue. In-game that is Novig (at most 0.75¢); pregame it is Novig at 0¢.
  - Exploiting gaps on thin futures and props.
- **NY legal risk for a NY resident:** NY has sued Kalshi, Polymarket, Novig (which is suing NY), Coinbase and Gemini. Account access could be cut off suddenly. That is a tail risk for inventory held on any single venue.

### Gaps
- No Crypto.com sports event-contract fee or NY availability data was found.
- No systematic, published frequency of Kalshi versus Polymarket sports arbitrage was found.
- No Galaxy, Paradigm or a16z 2026 sports market-share research was retrieved.
- Volume figures are not comparable across venues: Kalshi counts contracts at $1 notional, while Polymarket counts dollars paid.
