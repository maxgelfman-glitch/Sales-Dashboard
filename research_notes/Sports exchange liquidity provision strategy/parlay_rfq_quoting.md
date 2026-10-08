# Parlay / Combo RFQ Quoting as a Liquidity-Provider Strategy (Kalshi, Novig, Polymarket US, Robinhood, Underdog) — as of 2026-10-08

Method note: direct page fetches (docs.kalshi.com, docs.novig.com, sportico.com, prospect.org) were blocked from this research environment (DNS/proxy 403). Findings below come from search-engine extracts of those pages plus secondary coverage. Numbers flagged "unverified" could not be checked against the primary page. Every number should be re-checked against the live source before use in a trading decision.

## 1. Kalshi combos / MVE and the RFQ mechanism (flow, quoters, margins, retail P&L, volume share, fees)

### Takeaway
Kalshi combos are priced only by RFQ: a taker's request is broadcast to all makers, makers send private two-sided quotes, the taker can only accept the best one, and the maker then has a short confirm window (last look). In 2026 a small group of automated quoters (about 5 combo-only bots in a May 2026 sample) earned an estimated ~15% hold on retail combo flow. Kalshi added a maker fee on combos on 2026-08-20 (half the taker rate, uncorrelated NFL parlays exempt), which took about $26M in its first four weeks. Combos were about 58% of Kalshi notional volume in September 2026 but under 13% of transactions.

### Cited Findings
**Mechanics (Kalshi docs and rule filing)**
- RFQ flow: the requester posts an RFQ for a market and size. "The RFQ is broadcast to all makers. Makers respond with quotes containing a yes_bid and no_bid. Quotes are for the full RFQ size." Each quote is private between the requester and that maker, and makers cannot see each other's quotes. — [Kalshi RFQ docs](https://docs.kalshi.com/getting_started/rfqs)
- Execution is two steps: the requester accepts one side of the best quote (PUT `/communications/rfqs/{rfq_id}/quotes/{quote_id}/accept`, returns 204), then the maker confirms (PUT `.../confirm`), which "starts a timer for order execution". After the timer, the orders go into the book, and fills show in GET /portfolio/fills keyed on `creator_order_id` for the maker and `rfq_creator_order_id` for the requester. — [Kalshi accept endpoint](https://docs.kalshi.com/api-reference/communications/accept-rfq-quote.md); [Kalshi confirm endpoint](https://docs.kalshi.com/api-reference/communications/confirm-rfq-quote.md)
- Combo RFQs carry `mve_collection_ticker` and `mve_selected_legs`. Valid combinations are discovered through the Multivariate Event Collections endpoints. Quote events (quote_created, quote_accepted, quote_executed) go out on the communications WebSocket channel, only to the parties involved. — [Kalshi RFQ docs](https://docs.kalshi.com/getting_started/rfqs.md)
- Combo markets are classified as "High Volatility Markets (HVM), which have shorter timing windows." — [Kalshi RFQ docs](https://docs.kalshi.com/getting_started/rfqs)
- **Last look, per the CFTC rule filing:** in an HVM, the quoter has "only 1 second (or another period of time as set forth in the Kalshi Platform) to confirm" after acceptance. In non-HVM markets the window is 30 seconds, after which the quote is void. Once the quote is confirmed, a 15-second timer starts and "neither party can opt-out." — [Kalshi RFQ rule filing, CFTC (Jan 2026)](https://www.cftc.gov/sites/default/files/filings/orgrules/26/01/rules01302638537.pdf); 30-second figure also in [Kalshi FIX RFQ messages](https://docs.kalshi.com/fix/rfq-messages)
- A third-party developer tool reports that combo makers stand behind a quote for about 3 seconds, with execution about 1.1 seconds after confirmation. This is user-built and not verified by Kalshi. — [kalshi-cockpit GitHub issue #67](https://github.com/josephsapinoso/kalshi-cockpit/issues/67)
- Docs conflict on who confirms: the REST accept page says "the quoter" confirms, and the other pages say the maker. These are likely the same party, but test in the demo environment. — [Kalshi accept endpoint](https://docs.kalshi.com/api-reference/communications/accept-rfq-quote.md)
- A maker can quote a yes-bid or no-bid of 0 to decline one side, but not both. — reported via [Sportico explainer, Nov 2025](https://www.sportico.com/business/sports-betting/2025/kalshi-parlay-combo-rfq-explainer-1234877038/) and [Kalshi RFQ docs](https://docs.kalshi.com/getting_started/rfqs)
- Retail in-app users can only take a combo price. There are no limit orders on parlays, and building RFQs yourself requires API access. Cashing out right after purchase usually returns a sell price well below cost. — [Sportico, Nov 2025](https://www.sportico.com/business/sports-betting/2025/kalshi-parlay-combo-rfq-explainer-1234877038/)
- If no maker quotes, the combo does not fill. — [Kalshi Help Center: Combos](https://help.kalshi.com/en/articles/13823820-combos) (via search extract)

**Who the quoters are, and how many**
- Oddpool sent 270 small test RFQs to production Kalshi over two evenings in late May 2026, all auto-cancelled. The public RFQ feed showed about 12,178 RFQs in 90 seconds, roughly 135 per second, with one bot generating about 10% of requests. Two "backbone" bots answered about half of all requests. Thirteen automated quoters responded to sports requests: 8 quoted only single markets and 5 quoted only combos, with no overlap. Non-sports RFQs drew no quotes at all. Quotes came back in under 0.5 seconds, except one combo specialist that took up to about 4 seconds. — [Oddpool, "Mapping Kalshi's RFQ Layer" (2026)](https://www.oddpool.com/research/kalshi-rfq-market-makers)
- Susquehanna International Group (SIG) is named as one of the firms behind Kalshi's third-party oddsmakers. A SIG spokesperson declined to confirm that it provides parlay liquidity. — [Sportico, 2026](https://www.sportico.com/business/sports-betting/2026/kalshi-parlays-retail-bettor-losses-rfq-1234894471/)
- **Jump Trading:** I found no source tying Jump to Kalshi combo quoting. Treat that link as unverified.
- **RFQ counterparty IDs:** RFQs (unlike the anonymous order book) carry a pseudonymous creator ID. Responders can therefore profile repeat requesters and widen or withhold quotes for consistent winners. — [Whirligigbear Substack (A. Courtney)](https://whirligigbear.substack.com/p/are-traders-on-kalshi-being-profiled)
- A November 2025 rule amendment letting members hide their ID until after execution is reported only by a low-quality third-party site. Unverified.

**Retail P&L and implied margin**
- Retail takers lost about $117M on Kalshi parlays from 2026-01-01 to 2026-04-30, based on Dune public data. At least about $35M of that went to Kalshi as estimated fees. The losses were about 15% of cost basis, i.e. roughly $15 lost per $100 wagered. Sportico says the figure likely understates typical retail losses, because a small group of sharp takers offsets part of them. — [Sportico, 2026](https://www.sportico.com/business/sports-betting/2026/kalshi-parlays-retail-bettor-losses-rfq-1234894471/)
- Analyst comparison: sportsbook parlay margin was 19% in April 2026 (up from 17% in 2023), against Kalshi's implied combo margin of **14.7%**. Kalshi's exchange and third-party oddsmakers split the roughly 15% "hold." — [Sportico, 2026](https://www.sportico.com/business/sports-betting/2026/kalshi-parlays-retail-bettor-losses-rfq-1234894471/) (via search extracts); [Casino.org](https://www.casino.org/news/prediction-market-combos-bigger-losers-for-bettors-than-sportsbook-parlays/)
- **$294M net retail losses on Kalshi parlays** is attributed by many crypto outlets to Bloomberg, but the time period is not specified. I could not access the Bloomberg original. — [Prediction News](https://predictionnews.com/story/reddit-user-asks-kalshi-traders-about-their-biggest-losses); [CryptoNews.net](https://cryptonews.net/news/finance/33217091/). Treat as secondhand.
- Other estimates: combos are 36% of Kalshi trades, and retail loses 19 cents per dollar on combos against 6 cents on straight bets. The bottom quartile of users loses 28 cents per dollar. — via [The American Prospect, 2026-08-26](https://prospect.org/2026/08/26/house-always-wins-kalshi-prediction-markets/) (search extract; methodology not verified). A "25% edge" headline at [rg.org](https://rg.org/news/gambling-industry/kalshi-record-volume-longshot-parlays-house-edge) (methodology unverified). Kalshi's Dune data partner reportedly took the underlying data down and later put it behind a roughly $40k paywall, which limits independent verification. — via [American Prospect](https://prospect.org/2026/08/26/house-always-wins-kalshi-prediction-markets/)
- Retail takers lose more often than not, and Kalshi's oddsmakers profit. — [SI, "Who is really winning on Kalshi parlays"](https://www.si.com/betting/prediction-market/kalshi/who-is-really-winning-on-kalshi-parlays-according-to-the-data); [Sportico, Nov 2025](https://www.sportico.com/business/sports-betting/2025/kalshi-parlay-combo-rfq-explainer-1234877038/)
- Academic: an ifo/CESifo 2026 working paper, "Makers and Takers: The Economics of the Kalshi Prediction Market," exists. Its findings were not extracted here. — [ifo](https://www.ifo.de/en/cesifo/publications/2026/working-paper/makers-and-takers-economics-kalshi-prediction-market)

**Volume share**
- **September 2026:** combos were 58% of Kalshi notional volume but under 13% of transactions (Dune data). The gap exists because each $1-notional contract counts toward volume, so cheap long-shot combos inflate notional. — [CNBC, 2026-10-06](https://www.cnbc.com/2026/10/06/prediction-market-combo-contract-volume.html)
- September 2026 overall: Kalshi posted a record of about $60B notional. Combos drove 85% of notional growth and 46% of taker gains. — [Prediction News](https://predictionnews.com/story/kalshi-volume-surges-but-rivals-gain-market-share)
- In the week before 2026-08-20, combos were 43.5% of Kalshi volume (about $340M) against 4.6% of Polymarket US volume (about $40M). — [InGame](https://www.ingame.com/polymarket-us-launches-parlays/)
- Another source reports a record week of about $3.7B, with parlays at 68% of volume. — [rg.org](https://rg.org/news/gambling-industry/kalshi-record-volume-longshot-parlays-house-edge) (unverified)

**Fees for quoters**
- Until August 2026, Kalshi charged makers nothing on combos. From **2026-08-20**, the combo maker fee is **half the taker fee rate**, higher than most other Kalshi maker fees. Uncorrelated NFL parlays are exempt. Traders flagged the change before Kalshi announced it. — [InGame](https://www.ingame.com/polymarket-us-launches-parlays/); [CasinoBeats, 2026-08-18](https://casinobeats.com/2026/08/18/polymarket-launching-parlays-in-us-as-kalshi-looks-to-cash-in-on-combos/)
- The combo maker fee brought in about **$26M in its first four weeks**; another report says $25M. — [Bitcoin.com News](https://news.bitcoin.com/igaming/kalshis-parlay-maker-fee-brought-26-million-four-weeks/); [Bitcoin.com News](https://news.bitcoin.com/igaming/polymarket-us-tests-parlays-as-kalshi-banks-25m-in-fees/)

### Inferences
- Rough economics: if about $340M a week of combo notional carries roughly 15% gross hold, the gross edge pool is on the order of $50M a week, split between Kalshi fees and maybe 5 quoters. This is crude, because notional is not stake. The $26M in four weeks of maker fees (about $6.5M a week) shows Kalshi is now taking a large slice of the quoters' margin. Quoter net margin is probably well below the 14.7% gross.
- The 1-second HVM confirm window is a real last-look option for the maker: price stale or news arrived → don't confirm. Abuse is limited only by the short window and by Kalshi presumably monitoring confirm rates. No public "minimum confirm ratio" rule was found.
- Pseudonymous requester IDs allow per-counterparty pricing. This is the main defense against sharp parlay takers, and a new entrant lacks the history incumbents already have.
- Because the taker can only accept the best price, a new quoter wins only when it is the tightest. That is pure winner's-curse exposure (see section 3).

### Gaps
- Exact current HVM timing values in Kalshi's docs (1 s in the filing vs. about 3 s observed). Exact combo maker fee formula and coefficient.
- Any eligibility requirement to quote on Kalshi (market maker agreement vs. any API user). Oddpool's bots suggest ordinary API members can quote, but I found no explicit rule.
- Names of quoters other than SIG. Per-quoter P&L. Jump's involvement.
- The time window behind the $294M figure.

## 2. Novig RFQ LP program (registration, deposit, auction, last look, fee, COMBO volume)

### Takeaway
Novig's official fee page confirms a separate RFQ schedule for combination contracts. The fee is 0.10 × w·k/(w+k) (w = taker wager, k = pricer collateral), charged on execution. RFQ makes earn no Maker Credits. I could not verify the LP onboarding specifics in the brief ($30k deposit, 3-second auction, 1-second last look) or COMBO volume from data.novig.com, because docs.novig.com could not be fetched and search did not surface those pages.

### Cited Findings
- Novig's fee page: quoting an RFQ makes you the maker on a combination contract. The RFQ taker coefficient is **0.10**, against **0.03** for straight contracts. The fee is **0.10 × w·k/(w+k)**, where w is the taker's wager, k is the pricer's collateral, implied probability P = w/(w+k), and the pot is w + k. RFQ trades "are charged on execution, live or not." Combination contracts are **excluded from the Maker Credit Program**: only straight-contract makes matched in-game earn credits. — [Novig API: Trading Fees](https://docs.novig.com/fees)
- The same page also shows "RFQ (combination contracts): none" in one line, most likely the maker-side fee. The extract was ambiguous. — [Novig API: Trading Fees](https://docs.novig.com/fees)
- The Maker Credit Program is limited to qualifying trades in Live Markets and Eligible Futures Markets. Novig specifies by notice whether RFQ trades under **Rule 5.2** qualify. — [Novig Support: Maker Credit Program](https://support.novig.com/en/articles/16116780-maker-credit-program)
- The fee page lists an "LP onboarding" program but did not expose its terms in search. — [Novig API: Trading Fees](https://docs.novig.com/fees)
- **Unverified (from the research brief, not confirmed by any source found):** registration required, a $30,000 deposit, a 3-second auction, a 1-second last look, and COMBO rows on data.novig.com.

### Inferences
- The fee formula equals 0.10 × P × (1−P) × pot. Writing it as w·k/(w+k): for a long-shot parlay at P = 0.10 with a $10 wager, k = $90, and the fee is 0.10 × 900/100 = $0.90, or 9% of the taker's stake. The taker-side fee as a share of stake is 0.10 × (1−P), so it is heavy on long shots. Because the taker pays it, the quoter's competitive margin must sit on top of an already large taker cost. That reduces taker demand elasticity at the margin, but it does not cost the quoter directly.
- With no Maker Credits on RFQ, Novig combo quoting is a pure spread business with no rebate subsidy.
- If the 1-second last look is accurate, it mirrors Kalshi's HVM rule.

### Gaps
- LP onboarding terms, deposit, auction length, last-look duration and eligibility. Need a direct read of docs.novig.com/lp-onboarding and api-reference/rfq/*.
- COMBO volume time series from data.novig.com. No public commentary on Novig combo volume was found.

## 3. Pricing theory: correlated parlays, sportsbook SGP hold, winner's curse with N competitors, leg-error compounding

### Takeaway
Sportsbooks price same-game parlays (SGPs) by simulating the joint outcome and then adding large correlation and margin loads. Hold is about 15–25%+ (NJ parlay hold was 24.2% in September 2024). A competitive RFQ auction strips most of that load: Kalshi's implied 14.7% versus 19% at sportsbooks. In a best-price-wins auction, the winning quote is systematically the one that most underestimates the true probability. Optimal markup must therefore rise with model error and, in common-value settings, with the number of competitors.

### Cited Findings
- SGP hold estimates: 15–25%, sometimes higher. Another estimate is 15–30% with stacked correlation premiums. Straight bets run about 4–5%. — [OddsIndex SGP correlation guide](https://oddsindex.com/guides/same-game-parlay-correlation); [iGaming News](https://www.igamingnews.biz/same-game-parlay-sportsbook-margin-engine/) (affiliate/blog sources)
- New Jersey reported **parlay hold of 24.2% in September 2024**, against 4.4% on all other bet types combined. — via [iGaming News / NJ DGE data](https://www.igamingnews.biz/same-game-parlay-sportsbook-margin-engine/)
- SGPs are an estimated 35–40% of US sportsbook GGR. AGA blended hold rose from about 7.0% in 2019 to 10.2% in 2025. — [iGaming News, 2026](https://www.igamingnews.biz/same-game-parlay-sportsbook-margin-engine/)
- Books simulate thousands of game scenarios to get the joint probability. Example: a 3-leg SGP worth +596 if independent was offered at +350. The same SGP can be +650 at one book and +850 at another. — [Wizard of Odds](https://wizardofodds.com/article/same-game-parlays-the-mathematics-of-correlation/); [OddsIndex](https://oddsindex.com/guides/same-game-parlay-correlation)
- Open-source example: a project fits implied correlation (ρ) from Kalshi sports parlay RFQ quotes. — [lobster-market-pricing PR #347](https://github.com/rlancer/lobster-market-pricing/pull/347)
- Winner's curse: in common-value auctions, winning is bad news about your estimate, so rational bidders shade more than naive ones. Competition magnifies adverse-selection corrections. — [Bergemann, Brooks & Morris, "Countering the Winner's Curse"](https://econpapers.repec.org/paper/cprceprdp/13332.htm); [Winner's Curse Corrections Magnify Adverse Selection](https://mospace.umsystem.edu/xmlui/handle/10355/2383)
- Optimal market-maker quoting under adverse selection adds a global adverse-selection term plus a per-client-tier term. This maps to per-requester-ID pricing on Kalshi. — [arXiv 2508.20225, "Optimal Quoting under Adverse Selection and Price Reading"](https://arxiv.org/pdf/2508.20225)

### Inferences (own analysis; standard theory, not sourced to a specific paper)
- **Leg-error compounding:** for an n-leg parlay with leg probabilities pᵢ, relative errors roughly add: δP/P ≈ Σ δpᵢ/pᵢ. With 2% relative error per leg, a 6-leg parlay carries about 12% relative error. That is the same order as the whole 14.7% margin, so leg-model precision is the core edge.
- **Correlation error:** for two legs, P(A∩B) = pA·pB + ρ·√(pA(1−pA)pB(1−pB)). At pA = pB = 0.5, each 0.1 of ρ error moves the joint probability by 0.025 on a base of 0.25, a 10% relative error. Same-game legs (team ML + over, QB yards + team win) are where competitors disagree most. Disagreement is what the winner's curse feeds on.
- **Winner's curse with N quoters:** if each quoter's fair-value estimate carries independent noise σ, the best of N quotes is biased toward underpricing by roughly σ·E[max of N standard normals]: about 0.56σ at N = 2, 1.03σ at N = 3, 1.16σ at N = 5, 1.54σ at N = 10. With about 5 combo bots (Oddpool), a quoter needs a margin cushion of about 1.2σ above its own error just to break even on the fills it wins. Sharp takers make this worse, because they choose which combos to request.
- **Sportsbook comparison:** the hold gap (19% book vs 14.7% Kalshi, and ~24% NJ) is the room competitive quoting has removed. Further entry should compress it further, and Kalshi's new maker fee takes part of what is left.

### Gaps
- No academic paper specific to SGP or copula pricing in exchange RFQs was found. No published per-leg-count hold for Kalshi combos.
- No public data on quoted margin dispersion across Kalshi quoters.

## 4. Risks: concentration, sharp parlay bettors, last-look rules, capital tie-up, settlement and voids

### Takeaway
The main risks are (a) adverse selection from takers who exploit correlation mispricing and are profiled only by pseudonymous ID, (b) correlated exposure, since retail piles onto the same popular legs (NFL favorites, star props), (c) collateral equal to the full potential payout locked until the last leg settles, and (d) void/DNP rules that differ by leg and are inconsistently described.

### Cited Findings
- A combo pays at most $1.00 per contract. Payout equals the product of the legs' settlement values, so one losing leg zeroes it. — [PredictionMarketsPicks citing Kalshi Help Center](https://predictionmarketspicks.com/articles/how-kalshi-combos-work)
- Settlement comes after all legs resolve, typically 1–12 hours after the last leg. The combo stays open until then. — [Tech-Insider](https://tech-insider.org/prediction-markets/how-does-kalshi-payout-work/); [kalshi-parlays.com](https://kalshi-parlays.com/combos-guide) (secondary sources)
- Void and DNP treatment is inconsistent across sources. One says a void leg resolves in the holder's favor. Another says the leg's own market rules govern. A third says a DNP leg settles at the last fair traded price before the news, with no combo refund. — [kalshi-parlays FAQ](https://kalshi-parlays.com/faq); [OddsShopper on leg voids](https://www.oddsshopper.com/articles/prediction-markets/kalshi-parlay-leg-voids); [PredictionMarkets.us injury/DNP rules](https://predictionmarkets.us/articles/kalshi-player-injury-dnp-rules-explained). Unverified against Kalshi rules.
- No official maximum leg count on Kalshi was found. Most combos have 2–3 legs, and liquidity thins beyond 3–4. Polymarket US and Robinhood cap combos at 10 legs. — [PredictionsMarketFans](https://predictionsmarketfans.com/platform-reviews/how-do-kalshi-combos-work-the-complete-guide-to-kalshi-combo-bets-parlays-rules-); [InGame](https://www.ingame.com/polymarket-us-launches-parlays/); [BettingUSA](https://www.bettingusa.com/prediction-markets/combos/)
- A small group of sharp takers offsets part of retail losses on Kalshi parlays. — [Sportico, 2026](https://www.sportico.com/business/sports-betting/2026/kalshi-parlays-retail-bettor-losses-rfq-1234894471/)
- Quoters can refuse to answer, or widen quotes for, specific requester IDs. Declining by one quoter does not stop others from quoting. — [Whirligigbear](https://whirligigbear.substack.com/p/are-traders-on-kalshi-being-profiled)
- A developer issue notes that taking a maker's quote can consume "up to 90% of the combinations shard," illustrating size and concentration constraints. — [kalshi-cockpit issue #62](https://github.com/josephsapinoso/kalshi-cockpit/issues/62) (context unclear)
- Kalshi's data partner restricted public data access, reportedly with a roughly $40k paywall. — via [American Prospect](https://prospect.org/2026/08/26/house-always-wins-kalshi-prediction-markets/)
- Regulatory and reputational pressure: press coverage frames combos as a "house" business on a "house-less" exchange. — [American Prospect, 2026-08-26](https://prospect.org/2026/08/26/house-always-wins-kalshi-prediction-markets/)

### Inferences
- **Capital:** selling a combo priced at P means posting (1−P) per contract. On a 10% long shot, that is 90¢ of collateral to earn roughly 1.5¢ of expected margin, about a 1.7% return per turn, locked for hours to days. Annualized ROC depends on turnover. NFL Sunday combos tie up capital for about one day and futures-leg combos much longer. Avoid long-dated legs or price in the carry.
- **Concentration:** retail flow is correlated (same favorites, same "anytime TD" stars), so the book is effectively short a few popular outcomes. Per-leg exposure limits across all open combos are essential. One upset can wipe out many tickets' worth of margin at once.
- **Last-look abuse risk is two-sided:** the platform could penalize low confirm rates, and the maker's own stale quotes are risky if leg prices move during the up to 1–3 second window plus the 15-second execution timer.
- Rule and fee changes have appeared without notice (the 2026-08-20 maker fee was flagged by traders first), so fee regime risk is material.

### Gaps
- Official Kalshi void and DNP rules for MVE legs.
- Any published penalties for declining to confirm (last-look abuse).
- Collateral netting across correlated combos (whether Kalshi offers portfolio margin for makers).

## 5. Comparable venues and practitioner evidence (Polymarket US, Robinhood, Underdog, commentary)

### Takeaway
Combos are spreading across the US. Polymarket US launched public parlays in August 2026, also via RFQ with up to 10 legs, and they reached about 50% of its daily volume in the NFL season. Robinhood and Underdog route Kalshi combos to their own retail audiences. This widens Kalshi's taker funnel, and Polymarket US is a second venue to quote.

### Cited Findings
- Polymarket US: an API-only multi-leg beta started 2026-08-05, and an app launch came about 2026-08-20/21 with a "build a combo" button, up to 10 legs, and RFQ pricing. — [InGame](https://www.ingame.com/polymarket-us-launches-parlays/); [InGame test launch](https://www.ingame.com/polymarket-us-test-launches-parlays/); [Lines](https://www.lines.com/prediction-market/polymarket-combo)
- Combos were about 4.6% of Polymarket US volume at launch and nearly 50% of daily Polymarket US volume during the NFL season, per CNBC. — [InGame](https://www.ingame.com/polymarket-us-launches-parlays/); [CNBC, 2026-10-06](https://www.cnbc.com/2026/10/06/prediction-market-combo-contract-volume.html)
- Polymarket pays volume rebates to makers trading $10M+ a month (and takers $1M+), effective 2026-10-06. — [InGame/Polymarket fees](https://www.ingame.com/polymarket-reveals-us-fees/) (via search extract)
- Robinhood: preset NFL combos (winner + total) from 2025-12-17, custom combos up to 10 legs by the January 2026 playoffs, powered by its Kalshi partnership. Not available in MD, NJ or NV. — [DeFi Rate](https://defirate.com/news/robinhood-unveils-combos-and-ai-assistant/); [CasinoBeats, 2025-12-17](https://casinobeats.com/2025/12/17/robinhood-nfl-parlays-prop-bets-prediction-markets/); [Axios](https://www.axios.com/2025/12/17/ai-robinhood-stock-market)
- Underdog: "Prediction Parlay" (event contracts only) and "Combo Entry" (Kalshi contracts mixed with pick'em), both sourcing combos from Kalshi. — [BettingUSA](https://www.bettingusa.com/prediction-markets/combos/)
- Commentary: Sportico explainers (Nov 2025, 2026), the American Prospect (2026-08-26), CNBC (2026-10-06), InGame fee coverage, Oddpool's census, and the PM Pulse newsletter ([PM Pulse 057](https://pmpulse.substack.com/p/the-pm-pulse-057)). I found nothing specific from The Closing Line, Event Horizon, Front Office Sports or Reddit on quoter economics in this pass.

### Inferences
- Robinhood and Underdog flow into Kalshi RFQs, which adds recreational (low-information) takers. That is favorable for quoters relative to API-originated RFQs.
- Polymarket US is earlier-stage with likely fewer quoters, which may mean a wider winning margin. Its maker rebates need $10M+ a month to qualify.

### Gaps
- Number and identity of Polymarket US combo quoters, and their fee terms for combo makers.
- Whether Robinhood or Underdog flow is identifiable by requester ID on Kalshi.
- Podcast and X-thread practitioner evidence was not reachable in this pass.
