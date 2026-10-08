# Practitioner Reports on Automated Bots in Sports Prediction Markets & Exchanges (Kalshi, Polymarket, Novig, ProphetX, Betfair), as of Oct 2026

**Method note (read first):** These notes come from about 15 web searches and fetches done on 2026-10-08. Several primary pages could not be fetched because of DNS failures in the research sandbox: oddpool.com, predictionnews.com, ingame.com and dev.to. Facts from those pages come from search-engine summaries, so they should be re-verified before anyone quotes them as figures. Most of the "how-to" content online comes from bot or VPS vendors (turbinefi, clawarbs, startpolymarket, polymarkets.co.il, laikalabs, botblog). Those sources are marked **[VENDOR]**. Self-reported P&L is marked **[UNVERIFIED]**. I found no Reddit, X or Discord post with a verifiable, audited track record for a *sports market-making* bot.

---

## 1. Who reports profitable sports MM/arb bots in 2025-2026, at what scale, with what verification? Typical monthly returns for ~$100k operators?

### Takeaway
The large, credible profits are concentrated in a few institutions on Kalshi (Susquehanna and roughly a dozen RFQ makers) and in a few Polymarket wallets that are visible on-chain. These wallets are mostly directional sports traders, not confirmed market makers. Retail bot P&L claims are small (a few thousand dollars) and unverified. I found **no credible source for typical monthly returns of a ~$100k sports MM operator**. The only figures are vendor claims of $200-800/day on $50k, which works out to roughly 12-48%/month and should be read as marketing, not evidence.

### Cited Findings
**Institutional / platform level**
- Kalshi co-founder Luana Lopes Lara said in late Nov 2025 that the in-house arm, Kalshi Trading, is "not profitable." She said it gets no preferential access. InGame estimated it traded about $310M in sports in a month, assuming it is the maker on about 6% of sports trades. That $310M is InGame's estimate, not a disclosure. — [Yahoo Finance/InGame](https://finance.yahoo.com/news/kalshi-co-founder-says-house-174507266.html); [InGame](https://www.ingame.com/kalshi-in-house-trading-arm-not-profitable/)
- Susquehanna (SIG) became Kalshi's first "dedicated institutional market maker" in April 2024 and is believed to be the largest external maker. — [InGame via search summary](https://ingame.com/susquehanna-biggest-sports-kalshi-volume-knicks)
- **Key datapoint on maker losses (June 2026):** InGame reported that SIG's prediction-market desk had its biggest-ever sports loss on the Knicks' Game 4 NBA Finals comeback. Makers on the game traded $119.9M and takers $87.0M, and **takers made $22.4M profit before fees**. Jeff Yass would not confirm SIG's loss. *Search summary only; the page was not fetchable.* — [InGame](https://ingame.com/susquehanna-biggest-sports-kalshi-volume-knicks)
- An Oddpool "RFQ census" (2026) reportedly found that **13 makers reliably answer sports RFQs on Kalshi**, and the same makers gave zero engagement on every non-sports market tested. It also reportedly logged a case of zero maker responses across 126 test requests at peak. *Search summary; page unfetchable; methodology unknown.* — [Oddpool](https://www.oddpool.com/research/kalshi-rfq-market-makers)

**Polymarket on-chain (verifiable in principle)**
- Sports-heavy wallets as of May 5, 2026: "swisstony" had about $7.8M profit, 97% of it from sports, and "kch123" had 87% of profits from sports. A Deadspin profile gives swisstony a win rate of about 54% with more than 1,000 open trades, but a net worth of about $1.2M. Lifetime profit and current balance are different metrics, which explains the gap. "RN1" reportedly showed a $9.49M margin. — [KuCoin News](https://www.kucoin.com/news/flash/top-polymarket-traders-use-three-distinct-strategies-to-earn-millions); [Deadspin](https://deadspin.com/prediction-markets/trending/top-5-best-prediction-market-sports-traders/)
- On Polymonit's April 2026 board, the top wallet (0x49244…) was #1 both overall and in sports, with +$6.29M profit on $24.5M volume in one month. That is a very high profit/volume ratio, which suggests directional or informed trading rather than spread capture. — [Polymonit](https://polymonit.com/leaderboard/april-2026/)
- Laika Labs [VENDOR] lists beachboy4 as the #1 sports trader at about $4.36M all-time (Feb 2026). — [Laika Labs](https://laikalabs.ai/prediction-markets/top-polymarket-traders)
- Caveat: leaderboards rank by P&L and do not label strategy. None of these sources shows that these wallets ran *market-making* bots. Predicts.guru shows that the top-volume wallet ("ferrariChampions2026") is *net negative*. — [Predicts.guru](https://www.predicts.guru/leaderboard)

**Retail bot claims [UNVERIFIED]**
- A Reddit user (@b00k13) claimed about $8,300 over 3 months (Jan-Apr) from arbitraging lagging Polymarket prices against sportsbook odds. A P&L screenshot was shared. — [PredictionNews summary](https://predictionnews.com/story/reddit-user-says-ai-coded-arbitrage-bot-made-5k-on-polymarket-93b950a4)
- Another Reddit user claimed about $5k from an AI-coded Python arb bot. A public wallet was shared but no technical details. — same source
- A Hacker News user described scraping sportsbook odds, de-vigging them, and posting limit orders on Polymarket esports. No P&L was given. — same source
- Vendor guides claim two-sided MM on $50k "has returned $200-800/day" in reward-eligible sports and political markets from 2024 to 2026. No source is given. — [startpolymarket [VENDOR]](https://startpolymarket.com/strategies/reward-farming/)
- An older profile (about 2025) describes a reward-farming bot that started at $10k, earned about $200/day, and peaked at $700-800/day. This was before later reward-rule changes. — [Polymarket news / Substack](https://news.polymarket.com/p/automated-market-making-on-polymarket)
- Liquidity-reward pools are very uneven. The largest paid about $10,000/day, the median market about $5/day, and there is a $1/day per-market minimum. — [startpolymarket [VENDOR]](https://startpolymarket.com/strategies/reward-farming/)

**Open-source bots**
- warproxxx/poly-maker (1.5k stars, 489 forks) warns: "Market making on Polymarket is competitive and can lose money" and calls itself "not a guaranteed-profitable product." It is post-only, skews quotes by inventory, widens spreads on toxicity, and has a daily-loss kill switch plus regime states (QUIET/TRENDING/EVENT/REDUCE_ONLY/HALTED). — [GitHub](https://github.com/warproxxx/poly-maker)
- botforkalshi lists more than 30 open-source Kalshi bot projects in 2026 [VENDOR]. — [botforkalshi](https://www.botforkalshi.com/blog/open-source-kalshi-bot-ecosystem)

**Broad wallet profitability**
- A London Business School and Yale study covered 1.72M accounts, 210k+ markets and about $13.76B volume. Only 3.14% of accounts were "skilled winners." Skilled traders plus market makers contributed more than 30% of revenue. The 67% of accounts classed as bad luck or bad skill bore the entire net loss. — [KuCoin summary](https://www.kucoin.com/news/flash/3-14-of-polymarket-traders-account-for-30-of-profits-67-of-users-bear-all-losses)
- Dune-based estimates of the share of profitable wallets range from 7.6% to about 16-17%. — [CryptoRank on X](https://x.com/CryptoRank_io/status/1993331676588499353); [TechFlow](https://www.techflowpost.com/en-US/article/31934)
- Solidus Labs found that fewer than 1% of wallets captured about half of profits in key political markets (Dec 2025-Feb 2026). — [CoinDesk, 2026-04-29](https://www.coindesk.com/markets/2026/04/29/a-tiny-group-is-winning-on-polymarket-as-under-1-of-wallets-take-half-the-profits)

### Inferences
- On the best evidence, *passive sports market making is not reliably profitable even for the best-resourced players*. Kalshi's own desk says it is unprofitable, and SIG took large losses on in-game swings. Profits accrue to takers with information or speed, and to makers who price very well and also collect fee rebates or incentives.
- Polymarket's top sports profits look directional (high profit/volume, about 54% win rates on large size). Do not cite them as evidence that MM works.
- For a ~$100k operator, honest monthly-return expectations cannot be sourced. Vendor numbers (12-48%/month) are implausible as steady state and are tied to liquidity-reward programs that change.

### Gaps
- No audited or on-chain-verified P&L for a *sports market-making* bot specifically.
- No primary Bloomberg article on Kalshi maker economics was retrieved.
- Oddpool and InGame pages were unfetchable, so numbers from them are summary-level.

---

## 2. Common failure modes

### Takeaway
The dominant documented failure is adverse selection: stale quotes get hit on news, lineups and in-game swings. Next come latency and order-delay mechanics, then infrastructure (API outages, stale reads, RPC rate limits). Fee changes in 2026 compressed taker-side arb margins. Settlement-criteria divergence is a documented cross-venue risk.

### Cited Findings
- **Adverse selection:** A reported Stanford Law study of 41.6M Kalshi trades (Apr 2026) found informed traders hitting makers' stale quotes, with makers widening spreads in response. The effect was strongest in single-name contracts, and sports lineup changes are cited as an example. *Seen only via a vendor blog; the paper itself was not verified.* — [turbinefi [VENDOR]](https://www.turbinefi.com/blog/why-prediction-market-trades-get-picked-off-2026)
- **In-game blowups:** The Knicks Game 4 episode above (takers +$22.4M pre-fee vs makers, June 2026). — [InGame](https://ingame.com/susquehanna-biggest-sports-kalshi-volume-knicks)
- **Latency:** Chicago-to-Kalshi round trip is about 10ms. Kalshi's public hosts sit behind CloudFront, so a ping of about 1ms does not measure the matching engine. Quote cancel-and-replace windows are in the hundreds of milliseconds [VENDOR]. — [turbinefi](https://www.turbinefi.com/blog/prediction-market-arbitrage-latency-speed-2026); [tradoxvps [VENDOR]](https://tradoxvps.com/best-vps-for-kalshi-trading-bots/)
- **Polymarket sports order delay:** the Gamma API has a `secondsDelay` field on sports markets (football 1, NBA 0 per one developer). This delays taker orders, and makers can pull quotes inside that delay. That creates leg risk for arbs and gives makers some protection. — [dev.to postmortem via search summary](https://dev.to/ekocam/6-things-that-broke-our-polymarket-trading-bot-before-it-made-a-dollar-4efi)
- **API outage / stale state:** On 2026-09-03 Polymarket had an incident with delayed open-order reads affecting the CLOB API and WebSocket. The cause was database replica lag. No bot losses were publicly tied to it. — [search summary of Polymarket status](https://casatrick.substack.com/p/polymarket-trading-bot-state-reconciliation) (a state-reconciliation article argues bots must reconcile their local order state with the exchange)
- **Bug postmortem (2025, non-sports):** an off-by-one in a pause window plus Polygon RPC rate-limiting left a bot trading on stale prices for an hour. It lost about $3.20 over 22 trades before a daily-loss kill switch halted it. — [polymarkets.co.il postmortems [VENDOR]](https://polymarkets.co.il/en/bots/polymarket-bot-mistakes-postmortems/)
- **Discovery bug:** a GitHub PR shows sports markets being crowded out of the API's first page by derivative events (inning winners, first-five) that share the start time. The fix sorted by volume. — [GitHub PR](https://github.com/pointlessbassmusic-byte/pointless-repo/pull/29)
- **Weekend thin-market exploitation:** a trader reportedly netted $233k on XRP markets by repeatedly draining MM-bot liquidity in thin weekend markets. These bots "treat every price tick the same," ignoring liquidity regime and settlement-time incentives. This was crypto, not sports, but the mechanism transfers. — [Yahoo Finance](https://finance.yahoo.com/news/polymarket-trader-nets-233-000-061703636.html)
- **Fee changes (2026):** Polymarket expanded fees on 2026-03-30, and daily revenue rose from $30-80k (Jan) to $550-700k (early Apr). — [DeFi Rate](https://defirate.com/news/polymarket-revenue-surges-10x-fee-rollout-usd-token-platform-overhaul-ahead/); [Pine Analytics](https://pineanalytics.substack.com/p/polymarket-fee-rollout). A guide [VENDOR] says the sports taker fee rate rose from 0.03 to 0.05 in July 2026 (max taker fee from $0.75 to $1.25 per 100 shares). Maker fee is 0 with a 15% maker rebate in sports, versus 25% in most categories. — [startpolymarket](https://startpolymarket.com/learn/polymarket-fees/). *Verify against official docs.*
- **Inventory risk:** "a single mistimed fill can offset weeks of accumulated rebate income." Getting stuck long the less-probable side is a common risk. — [laikalabs [VENDOR]](https://laikalabs.ai/prediction-markets/market-making-on-polymarket); [datawallet](https://www.datawallet.com/crypto/top-polymarket-trading-strategies)
- **Settlement divergence:** in the 2024 government-shutdown market, Polymarket resolved YES and Kalshi resolved NO under different criteria, which broke a hedged pair. — [trevorlasn blog](https://www.trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works)

### Inferences
- For sports MM, the biggest single risk factor is quoting through high-information moments: injury and lineup news, scoring swings, end-of-game. Practitioner-built bots respond with regime states (EVENT/REDUCE_ONLY) and toxicity-based widening (see poly-maker).
- Infrastructure failures are frequent but usually cheap *if* a kill switch and state reconciliation exist. Bots without them carry tail risk.

### Gaps
- No specific public report of a Kalshi account being limited for bot activity. Kalshi is an exchange and does not limit winners the way sportsbooks do. No reports were found for Novig or ProphetX either.
- No quantified "edge decay" time series for sports MM spreads.

---

## 3. Cross-venue arbitrage: frequency, duration, execution issues

### Takeaway
Gaps are frequent but short-lived. The best dataset (a vendor scanner, esports, Sep-Oct 2026) shows a median live-arb life of about 2s overall and about 4s when a Polymarket or Kalshi leg is involved. Pre-match exchange-leg arbs have a median of about 10s and a p90 of about 177s. After fees and slippage, edges are typically 1-2c, and leg risk is the top practical problem.

### Cited Findings
- 30-day esports scan (2026-09-06 to 2026-10-06), 27 books including Polymarket and Kalshi: about **4.87M arb opportunities**. Live arbs had a **median life of 2.0s** overall, **4.1s** with a Polymarket leg and **4.0s** with a Kalshi leg. Exchange-leg arbs are 1.7x more likely to survive 10s and 2.4x more likely to survive 60s in live play. Pre-match exchange-leg arbs: median 10.3s, p90 177.4s. *Vendor scanner, esports only, summary-level (dev.to unfetchable).* — [dev.to/dozor](https://dev.to/dozor/polymarket-kalshi-arbitrage-vs-sportsbooks-30-days-of-esports-data-1dc2)
- Vendor and anecdotal estimates conflict: "15-30 seconds on sports," "2-15 minutes," and "5-minute window in 2024 → 30 seconds in 2026." — [clawarbs [VENDOR]](https://clawarbs.com/blog/kalshi-vs-polymarket-arbitrage/); [launchpoly [VENDOR]](https://launchpoly.com/blog/polymarket-kalshi-arbitrage-guide); [predterminal [VENDOR]](https://predterminal.com/blog/kalshi-vs-polymarket-live-arbitrage-august-2026-whales-price-gaps-safe-trading)
- NBA Finals 2026: "sharp bettors make 1-2c per trade" between Kalshi and Polymarket. — [XCLSV Media](https://xclsvmedia.com/kalshi-vs-polymarket-arbitrage-2026-nba-finals-sharp-bettors/)
- Slippage example: a 1,000-contract order at $0.42 averages about $0.434, shrinking a 3c gap to about 1.6c. A "6% spread may net 1-2% after fees." — [clawarbs [VENDOR]](https://clawarbs.com/blog/polymarket-arbitrage-bot/)
- Operational frictions: one leg filling without the other is the "#1 risk." Fixes are staged small orders and a timeout or unwind if the second leg does not fill. Capital is split between USDC (Polymarket) and USD (Kalshi). — [trevorlasn](https://www.trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works); [avo.bet](https://www.avo.bet/articles/kalshi-arbitrage-betting)
- **Polymarket intra-venue arbitrage (academic):** Saguillo, Ghafouri, Kiffer and Suarez-Tangil (IMDEA, arXiv 2508.03474, Aug 2025; conference Oct 2025) estimate **about $40M realized arbitrage profit** from Apr 2024 to Apr 2025. They cover single-market "rebalancing" (YES+NO≠$1) and LLM-detected "combinatorial" arbs, with more than 7,000 markets mispriced. Most trades returned 1-5%. The top 3 wallets made about $4.2M on more than 10,200 bets. **Politics was more exploited than sports**, with the 2024 election dominant. Not peer-reviewed. — [arXiv](https://arxiv.org/abs/2508.03474v1); [Decrypt](https://decrypt.co/339958/40-million-free-money-glitch-crypto-prediction-markets); [DL News](https://www.dlnews.com/articles/markets/polymarket-users-lost-millions-of-dollars-to-bot-like-bettors-over-the-past-year/)

### Inferences
- Cross-venue sports arb is a low-latency, high-frequency, thin-edge business. A 4s median is enough for co-located automation, but not for manual or two-tab execution. Polymarket's sports `secondsDelay` and Kalshi's ~10ms RTT make simultaneous fills hard.
- Taker-fee increases in 2026 (Polymarket sports) push viable arbs toward maker-on-one-leg structures: rest on the slower venue and hedge on fill.
- The $40M IMDEA figure is mostly non-sports and pre-fee-era (2024-25). It is not evidence of current sports arb capacity.

### Gaps
- No sports-specific (NFL/NBA) gap-duration study. The best data is esports from a vendor.
- No data on Polymarket/Kalshi withdrawal-limit friction for arbers.

---

## 4. Polymarket on-chain analyses of MM/arb wallet profitability in sports

### Takeaway
No public Dune dashboard or paper isolates sports *market-maker* P&L. The available analyses are platform-wide (IMDEA $40M arb; LBS/Yale skill classification; Solidus concentration) plus per-wallet leaderboard snapshots.

### Cited Findings
- See IMDEA, LBS/Yale and Solidus above.
- A guide cites a 2025 study claiming market making was the only strategy class that strongly predicted positive returns, with collective MM profits above $20M in 2024. **I could not locate or verify this paper, so treat it as unverified.** — [search summary of vendor guide](https://startpolymarket.com/strategies/market-making/)

### Gaps
- No sports-specific maker/taker split, realized spread, or MM wallet P&L dataset was found. Building one from on-chain CLOB fills (maker vs taker address flags) is feasible but not publicly done in anything I found.

---

## 5. Betfair bot community lessons

### Takeaway
Long-running consensus on the Betfair and Bet Angel forums: buying a scalping bot almost never works, and shared bots lose their edge once widely used. Thin-margin scalping is hit hardest by Betfair's profitability charge, now described as the "Expert Fee" (from Jan 2025). Most forum evidence is about 2020 vintage, and 2025-26 material is mostly vendor blogs.

### Cited Findings
- Expert Fee (reportedly replaced the Premium Charge in Jan 2025): it applies if the account is lifetime-profitable, has more than £25k gross profit in the last 52 active weeks, and has bet in more than 100 markets. The rate is 20% on profit from £25k to £100k and 40% above £100k, net of commission paid. Another site still cites 250+ markets for the old Premium Charge, so the sources conflict and the official Betfair Help Centre should be checked. — [botblog [VENDOR]](https://botblog.co.uk/betfair-expert-fee-premium-charge/); [betfairsquare](https://betfairsquare.com/blog/betfair-premium-charge-guide-how-avoid)
- Thin-margin one-tick scalping and pre-off trading are the hardest hit, because the edge is small relative to a 20-40% charge. — [botblog [VENDOR]](https://botblog.co.uk/betfair-expert-fee-premium-charge/)
- Forum (c. 2020): the odds of profiting from a purchased scalping bot are "slim to none." One poster spent hundreds of hours and was still testing. With a one-tick offset, the entry fills and then the price "moves rapidly away," which is classic adverse selection. Widely shared bots stop working. Two one-tick bots lost money every day for a week. — [Bet Angel forum t=21452](https://forum.betangel.com/viewtopic.php?t=21452); [t=17907](https://forum.betangel.com/viewtopic.php?t=17907)
- A bot running 300-400 transactions per race earned under 5p commission on a winning race. Posters were concerned about Betfair's 1,000-transactions-per-hour limit and data charges. — [Bet Angel forum t=18407](https://forum.betangel.com/viewtopic.php?t=18407); [sports-arbitrage.com](https://sports-arbitrage.com/sports-betting/betfair-scalping-automated-bots/)

### Inferences
- Betfair's lessons transfer directly: one-tick passive scalping is adverse-selected, crowded strategies decay, and platform charges on *winners* can erase thin edges. Kalshi and Polymarket have no Expert Fee equivalent yet, but taker-fee hikes play a similar role.

### Gaps
- No 2025-26 forum thread with quantified Betfair bot P&L was found.

---

## 6. Risk controls, kill switches, sizing advice from operators

### Takeaway
The recurring advice converges on a standard set of controls:
- paper-trade first, then go live small;
- post-only quoting;
- inventory-skewed quotes;
- toxicity- and volatility-based spread widening;
- event and reduce-only regimes around news and game state;
- per-market and global exposure caps;
- daily-loss kill switches;
- strict state reconciliation with the exchange;
- staged legs with timeouts for arbs.

### Cited Findings
- poly-maker: post-only quoting, inventory skew, volatility/toxicity spread widening, regime state machine (QUIET/TRENDING/EVENT/REDUCE_ONLY/HALTED), daily-loss kill switch, exposure caps, "test in --paper mode first; go live with small size." — [GitHub](https://github.com/warproxxx/poly-maker)
- A kill switch capped a stale-data bug at about $3.20. — [polymarkets.co.il](https://polymarkets.co.il/en/bots/polymarket-bot-mistakes-postmortems/)
- State reconciliation is essential, given outages like the delayed open-order reads of 2026-09-03. — [casatrick Substack](https://casatrick.substack.com/p/polymarket-trading-bot-state-reconciliation)
- For arbs: stage orders smaller than the target until both legs fill, and stop or unwind if the second leg does not fill within N seconds. — [trevorlasn](https://www.trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works)
- Novig (CFTC DCM via Ludlow Exchange since 2026-06-16, with the regulated exchange live from 2026-08-04): makers pay no fees and may earn credits, and the taker fee is capped near $0.0075/contract according to a third-party review. Player-prop liquidity is the weakest. No official bot or API program was confirmed. — [Legal Sports Report](https://www.legalsportsreport.com/prediction-markets/novig-promo-code/); [bellwether](https://bellwether.market/platforms/novig/)

### Gaps
- No experienced operator published concrete sizing rules for sports MM, such as % of bankroll per market or max inventory per game. ProphetX: no practitioner bot reports found.
