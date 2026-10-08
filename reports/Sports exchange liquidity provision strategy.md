# Quote parlays, take stale prices, verify everything

**The evidence does not support $15–20k a month from a $100k bankroll on these venues. A $10k month is a stretch goal, reachable only if at least two liquidity engines pass live gates at once.** The three strategies have very different evidence behind them. Pregame taking of prices that beat a de-vigged Pinnacle fair value has the best support, but it is capacity-limited to roughly $1–3k a month. Cross-venue locked arbitrage is real but thin, worth hundreds of dollars a month. Pregame market making on Novig has no public track record either way. Parlay RFQ quoting is the only activity with a documented profit pool big enough to matter: retail takers lost about 15% of their stake on Kalshi parlays. That pool is contested by about five automated incumbents, and the winning quote is systematically the one that has most underpriced the risk. Two constraints bind before skill does. The first is collateral: selling a 15¢ parlay ties up 85¢ to earn about 1¢, so profit scales with capital, not cleverness. The second is the gambling tax rule. If these contracts are taxed as wagering, only 90% of gross losses are deductible, and a high-turnover book could owe tax on income it never earned. **The plan is therefore sequenced as gated experiments.** Fair-value validation through the taker comes first. Kalshi combo quoting focuses on cross-game NFL parlays, which carry no maker fee. Novig pregame making runs at tiny size and wide quotes. Novig RFQ onboarding follows only after its combo volume is measured. Kill rules are tied to closing-line value and markouts, not P&L, because a month of P&L is mostly noise at this size. New York legal and tax questions are prerequisites for scaling, not footnotes.

## Fees, rebates and access shifted in the last month

Four facts changed since the earlier research, and each one moves the plan.

**First, Novig's pregame fee question is resolved.** Novig's v3 documentation (snapshot dated 2026-09-28) charges takers c·P·(1−P) with c = 0.03 only while an event is in-game. Pregame trading is free for both sides. Makers never pay, and the 50% maker credit applies only to live fills ([Novig API docs](https://docs.novig.com/); [Novig fees](https://docs.novig.com/fees)). A 2.5% pregame edge on Novig therefore stays 2.5%, and a Novig taker leg adds no fee to a locked pair. The flip side matters as much. **Novig is the cheapest venue in the country for anyone to cross pregame.** So the counterparties hitting a Novig maker's quote include every sharp and arbitrage bot that finds a stale price.

**Second, the engine's no-in-play rule gives up every maker subsidy available to it.** Novig's maker credit is live-only ([Novig Support](https://support.novig.com/en/articles/16116780-maker-credit-program)). Kalshi's Sports Prop Combo leg program pays 25% of combo fees to makers in prop legs, but only on maker volume traded after the scheduled start ([Kalshi Help](https://help.kalshi.com/en/articles/17184676-sports-prop-combo-market-component-legs-liquidity-incentive-program)). Kalshi's standard Liquidity Incentive Program (LIP) still pays pregame, between $1 and $1,000 per market per day, scored from random once-per-second snapshots weighted by size and distance from the best price. Since 2026-02-28 it pays only when both sides are quoted, and it runs to 2027-01-01 ([CFTC LIP update](https://www.cftc.gov/filings/orgrules/rules07152610358.pdf); [CFTC amendment](https://www.cftc.gov/sites/default/files/filings/orgrules/26/02/rules02112639183.pdf)). Kalshi's Volume Incentive Program ends no earlier than 2026-10-13, amid wash-trading scrutiny ([LSR](https://www.legalsportsreport.com/279648/volume-rewards-program-on-kalshi-set-to-end-nearly-a-year-early/)). The no-in-play rule should still stand. **In-play is where makers lose the most.** Makers on Kalshi lost about $22.4M before fees on a single Knicks NBA Finals comeback, which was reportedly Susquehanna's largest sports loss ([InGame](https://ingame.com/susquehanna-biggest-sports-kalshi-volume-knicks)). Against that tail, the live maker credit is about 0.375¢ per contract at 50¢, which is not worth it.

**Third, Kalshi now taxes combo quoters.** From 2026-08-20, Kalshi charges combo makers half the taker rate. Uncorrelated NFL parlays are exempt. The fee took about $26M in its first four weeks ([InGame](https://www.ingame.com/polymarket-us-launches-parlays/); [Bitcoin.com News](https://news.bitcoin.com/igaming/kalshis-parlay-maker-fee-brought-26-million-four-weeks/)). At a 15¢ combo, half of 0.07·P·(1−P) is about 0.45¢ per contract, or **3% of the taker's stake**. That is a large slice of a quoter's net margin, so it should drive which combos the engine quotes.

**Fourth, Novig's RFQ liquidity-provider path is now concrete.** Per the coordinator-verified docs:
- Onboarding is by email, with a W-9, and QA access comes within two business days.
- Production generally requires a **$30,000 minimum deposit**.
- Each RFQ goes to all registered pricers for a 3-second auction. The lowest price wins, and a tie goes to the larger max_wager. The winner gets a 1-second last look.
- Quotes must be fully collateralized, with max_wager of at least the greater of $10 and the user's minimum wager.
- Parlays take 2–20 legs, and same-game legs are allowed.
- The RFQ taker pays 0.10·w·k/(w+k). The quoter pays nothing and earns no maker credit ([Novig fees](https://docs.novig.com/fees)).

The separate $200,000 threshold on Novig's fee page is for a dedicated Slack channel, not for quoting.

Access is settled for today but not for tomorrow. **The trader reports first-hand (October 8, 2026) that Novig is operational for them in New York.** One third-party review lists New York among Novig's restricted states ([casino.org](https://www.casino.org/us/predictions/novig/)), but the trader's account supersedes it. The live risk is regulatory. Novig sued New York pre-emptively in S.D.N.Y. the day after its launch ([Covers](https://www.covers.com/industry/novig-sues-new-york-day-after-launching-prediction-markets-aug-5-2026)), no ruling has been found, and New York has already sued Kalshi and Polymarket US. A loss, or a state action against Novig, could force a New York geofence. The engine will see that first as HTTP 451 on order placement, because Novig requires an app geolocation in an allowed state within the last three days. HTTP 451 should therefore trigger the exit mode described below, not a retry. A VPN is never a fallback: Novig refuses VPN connections, and Nevada's enforcement shows that geofence evasion is policed ([SBC Americas](https://sbcamericas.com/2026/07/28/kalshi-shuts-down-nevada/)).

All five research notes behind this report were built from search-engine extracts, because direct page fetches were blocked. **Treat every venue number as "verify against the live page before committing capital."** The Novig figures supplied by the coordinator are the exception, since they were read directly.

## Pregame market making earns only what fills against you do not take back

### Why the economics are thin

The theory is clear about structure and silent about numbers. Avellaneda-Stoikov quoting, adapted to binaries by working in log-odds, gives the shape. Skew the reservation price against inventory, widen with volatility and time to the next information event, and scale spreads by roughly p(1−p) away from 50¢ ([Dalen, arXiv 2510.15205](https://arxiv.org/html/2510.15205v2); [Feil & Nendel, arXiv 2607.17991](https://arxiv.org/pdf/2607.17991)). But the only empirical test on real order books fit badly and unstably. On some days the model-implied spread was **1.8 to 5.4 times the observed spread** ([University of Tartu thesis](https://dspace.ut.ee/items/b6ff7a3f-17e4-4009-86f6-899560034b3a)). Glosten-Milgrom supplies the number that matters. The half-spread must exceed the probability that a fill is informed times the price jump that follows. If 30% of fills precede a 3¢ Pinnacle move, a 2¢ half-spread nets only about 1.1¢. **That probability can only be measured from the trader's own markouts.** No published markout curves exist for Kalshi or Novig sports contracts.

The aggregate evidence favors makers over takers, but it does not transfer cleanly to this trader:
- On Kalshi, makers lost about 10% and takers about 32% on a contract-return basis, in a sample dominated by longshots ([VoxEU](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)).
- On Polymarket, winning traders are mostly limit-order providers, and the top 1% of profitable users capture 76.5% of profits ([CEPR DP21615](https://cepr.org/publications/dp21615)).
- On Kalshi, one-sided order flow predicts maker losses ([Bartlett, Stanford](https://law.stanford.edu/?p=564918)).

The maker subsidy in this literature comes from retail overbetting YES and longshots. That subsidy is weakest on near-even pregame sides and totals, which is exactly where a Pinnacle-anchored maker quotes.

The practitioner record is worse. Kalshi's own trading arm called itself "not profitable" ([InGame](https://www.ingame.com/kalshi-in-house-trading-arm-not-profitable/)). The most-used open-source Polymarket maker warns that it "can lose money" ([poly-maker](https://github.com/warproxxx/poly-maker)). Betfair forums describe one-tick scalpers that fill and then watch the price "move rapidly away" ([Bet Angel forum](https://forum.betangel.com/viewtopic.php?t=21452)). **No audited P&L for a small independent sports market-making bot exists anywhere in the evidence.**

### Venue choice: Novig, not Kalshi

On Kalshi, liquid NFL books sit 1¢ wide ([OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-vs-polymarket-nfl)), and spreads compress to about 1¢ roughly 48 hours before tip ([Blockworks Research](https://app.blockworksresearch.com/unlocked/from-betting-to-trading-how-kalshi-is-reshaping-sports-markets)). A maker capturing half of 1¢ while possibly paying a 0.44¢ maker fee nets close to zero before any adverse selection ([MarketMath](https://marketmath.io/blog/kalshi-fees-guide-2026)). Kalshi pregame making is therefore worth running only as an LIP play. That makes it a measurement task first: record the Rewards pool on NFL and NBA game markets for a week before writing any code.

Novig pregame removes the fees on both sides, so spread capture minus adverse selection is the whole P&L. The danger is selection. **A quote set wider than the prevailing Novig book fills mainly when fair value has already moved through it.** Wide quotes are therefore not automatically safe. They are filled disproportionately by informed flow. The second risk is a well-informed affiliate on the other side: Novig's affiliated trading arm, Manhattan Athletic Group, trades on the exchange behind an information barrier ([Sportico](https://www.sportico.com/business/sports-betting/2026/prediction-market-maker-affiliate-odds-1234884140/)).

Volume share is not the binding constraint. Novig's own data site reports about $41M a day ([data.novig.com](https://data.novig.com/)). Consider a maker netting 0.5¢ per contract at 50¢, which is 1% of stake. To make $5k a month it needs about $500k of monthly fills, or roughly $17k a day, which is a sliver of reported volume.

**Variance is the binding constraint.** A $1,000 net position at 50¢ held to settlement has a standard deviation of about $1,000. Across 17 games a day that is about $4k daily and about $20k monthly, which dwarfs a $5k expected profit. Keeping net inventory at kickoff near $250 per game cuts the monthly standard deviation to about $5k. So the maker's real job is to get two-sided fills to net out, not to accumulate positions. The engine's $1,000 per-game unhedged cap is a hard ceiling; the operating target should be a quarter of it.

### Recommended maker settings

Each setting traces to the evidence above.

| Parameter | Setting | Basis |
|---|---|---|
| Venue | Novig pregame only; Kalshi only after LIP pools are measured | Fees; Kalshi 1¢ books |
| Fair value | Power or Shin de-vig beyond 65/35; multiplicative inside 35–65 | Method choice moves fair value up to 2.4 points at the extremes ([SharkBetting](https://www.sharkbetting.com/blog/devig-explained)); proportional is adequate at 35–65 ([Datagolf](https://datagolf.com/how-sharp-are-bookmakers)) |
| Starting half-spread | 2.0¢ at 50¢, scaled by p(1−p)/0.25, never below 1.0¢ | Covers de-vig uncertainty plus assumed adverse selection; tighten in 0.25¢ steps only when the 5-minute markout is positive over 200+ fills |
| Lean | Quote favorites (≥50¢) more aggressively; never accumulate longshot YES | Contracts above 50¢ earn small positive returns, contracts under 10¢ lose more than 60% ([UCD WP2025/19](https://www.ucd.ie/economics/t4media/WP2025_19.pdf)) |
| Clip | $10 → $50 → $100 → $250 per quote, advancing only on gate passes | Canary discipline |
| Net inventory | Target ≤$250 per game at T−start; hard cap $1,000; $5,000 per slate or team cluster; $15,000 total | Variance arithmetic above |
| Expiry | Good-till-time (GTT) of 5 minutes, re-posted only on a fresh fair-value tick | Already in the engine |

Cancel rules matter more than spread settings, because a stale quote is the dominant way makers lose ([turbinefi summary of Stanford study](https://www.turbinefi.com/blog/why-prediction-market-trades-get-picked-off-2026)). The engine should cancel a game's quotes when any of these fires:
1. De-vigged Pinnacle fair value moves by 0.75¢ or more.
2. The TheRundown feed for that game is older than 5 seconds.
3. Two same-side fills arrive within 60 seconds. Bartlett's one-sided-flow signal applies here, and the market should be suspended for 5 minutes.
4. A fill arrives before a Pinnacle move in the same direction is seen. Log these as "picked off," and if more than 25% of the last 50 fills are picked off, halve the clip.
5. Scheduled news windows arrive. Pull NFL quotes from T−100 to T−80 minutes around inactives, and NBA quotes from T−40 minutes, with a full stop at T−10. Pull MLB quotes from lineup or pitcher confirmation until 5 minutes after. Pull soccer quotes around team sheets at about T−60.

The last rule extends the engine's 3-minute pre-start cutoff. **The 3-minute cutoff is too late.** The final 30–90 minutes carry the densest news, and insiders cluster in less liquid events and in early betting ([Shing 2005](https://econwpa.ub.uni-muenchen.de/econ-wp/fin/papers/0412/0412010.PDF)). Novig's GOLIVE event voids all resting orders. That is a backstop, not a control.

## Parlay quoting holds the only large pool, and it is guarded

### Who earns the pool, and why it is crowded

The gross pool is large. Retail takers lost about **$117M on Kalshi parlays** from January to April 2026, roughly 15% of cost basis. At least about $35M of that went to Kalshi as fees ([Sportico](https://www.sportico.com/business/sports-betting/2026/kalshi-parlays-retail-bettor-losses-rfq-1234894471/)). Kalshi's implied combo margin was about 14.7%, against 19% at sportsbooks (same source). By September 2026, combos were 58% of Kalshi notional volume and drove 85% of its notional growth ([CNBC](https://www.cnbc.com/2026/10/06/prediction-market-combo-contract-volume.html); [Prediction News](https://predictionnews.com/story/kalshi-volume-surges-but-rivals-gain-market-share)). Robinhood and Underdog route recreational combo flow into Kalshi RFQs ([BettingUSA](https://www.bettingusa.com/prediction-markets/combos/)). This is the only strategy with documented, persistent losses from low-information takers. That is the precondition for any liquidity-provision business.

The pool is guarded, though. Oddpool's census found **13 automated sports quoters on Kalshi, 5 of them combo-only**, with most quotes returned in under half a second ([Oddpool](https://www.oddpool.com/research/kalshi-rfq-market-makers)). Susquehanna is named among the oddsmakers ([Sportico](https://www.sportico.com/business/sports-betting/2026/kalshi-parlays-retail-bettor-losses-rfq-1234894471/)).

Requester IDs are pseudonymous, so incumbents can profile repeat sharp takers and widen against them ([Whirligigbear](https://whirligigbear.substack.com/p/are-traders-on-kalshi-being-profiled)). A new entrant has none of that history.

Only the best quote can trade, so a newcomer wins exactly when its price is the most generous. That is the winner's curse ([Bergemann, Brooks & Morris](https://econpapers.repec.org/paper/cprceprdp/13332.htm)). With five independent quoters, each with pricing noise σ, the best quote is biased by about **1.16σ** toward underpricing the risk. Leg errors compound as well. Relative errors roughly add across legs, so 2% per leg becomes about 12% on a six-leg parlay, about the size of the whole 14.7% margin. Same-game correlation errors are worse: each 0.1 error in ρ moves a 50/50 two-leg joint probability by about 10% relative.

### Where a new quoter can still win

These facts point to a specific niche: **cross-game NFL parlays of two to three legs on Kalshi**.
- **Fee:** cross-game parlays are uncorrelated by construction, so they are exempt from the maker fee. That leaves the quoter's full margin intact.
- **Model risk:** legs priced off Pinnacle game lines carry the least model error the engine has. There is also no correlation to estimate, which is where the winner's curse feeds hardest.
- **Capital efficiency:** shorter parlays use collateral roughly twice as well. Take a 6% relative margin. On a 30¢ two-leg combo that earns 1.8¢ on 70¢ of collateral, about 2.6% per turn. On a 15¢ three-to-four-leg combo it earns 1¢ on 85¢, about 1.2% per turn.

Same-game parlays and props should wait until the engine has a correlation model that has been checked against realized outcomes. Novig's RFQ is the second venue. There the quoter pays no fee, but the taker pays a heavy 0.10·P·(1−P)·pot, which is about 8.5% of stake at P = 0.15. That probably suppresses demand. Its value depends on volume, and Novig publishes COMBO rows in its public daily trade files, so volume can be measured before committing the $30k deposit.

Polymarket US combos reached nearly half of its daily volume during the NFL season ([CNBC](https://www.cnbc.com/2026/10/06/prediction-market-combo-contract-volume.html)), and there may be fewer quoters. But New York sued Polymarket US on 2026-09-24 ([NY AG](https://ag.ny.gov/press-release/2026/attorney-general-james-and-governor-hochul-announce-lawsuit-against-polymarket)), and its combo-maker terms are unknown. Keep it in watch-only status.

### Collateral, not skill, caps the upside

**Capital is the binding constraint on parlay income.** Selling a combo at price P locks (1−P) per contract until the last leg settles ([PredictionMarketsPicks](https://predictionmarketspicks.com/articles/how-kalshi-combos-work)). At a realistic net edge of 1–1.5¢ per contract on 70–85¢ of collateral, each turn returns about 1.2–2.1%.

At those rates, $10k a month from combos alone would take $500–850k of collateral turnover. That is about $20–35k locked every day for a full month, which consumes most of the bankroll. A $25–30k combo allocation at realistic utilization supports about $2–6k a month.

Variance is manageable only if exposures are diversified. One combo with a $100 pot at q = 0.14 has a standard deviation of about $35 against about $1 of expected value. Three thousand independent wins a month gives a respectable risk-adjusted profile. But retail piles onto the same favorites and star props, so the effective number of independent bets is much smaller. **Per-leg liability caps are the most important parlay control.**

### Recommended combo settings

The engine's current margin is 4% plus 2% per leg, which is 10% on a three-leg combo. That is too thin for non-exempt combos once the 3% maker fee and a winner's-curse cushion are added. It is plausible only for fee-exempt cross-game NFL combos. The quoted margin floor should be computed as:

**fee as a share of price + 1.2 × σ_model + 3% profit target**

Here σ_model is the measured relative error of the combo fair value against leg closing lines.

| Control | Setting |
|---|---|
| Scope, phase 1 | Kalshi cross-game NFL, 2–3 legs, all legs with Pinnacle prices; NBA cross-game, 2 legs, after 4 weeks |
| Margin floor | Formula above; start at 8% for exempt 2-leg, 10% for exempt 3-leg, at least 14% for any non-exempt combo |
| Requester filter | Track P&L and CLV per requester ID; stop quoting IDs whose won combos show combo CLV ≤ −5% over 20+ wins |
| Last look | Re-price every leg at confirm; decline if any leg's fair value moved ≥0.5¢ or the feed is older than 3 seconds; track decline rate and keep it under 10% |
| Per-combo liability (collateral k) | $50 canary → $250 → $500 → $1,000 maximum (about one-eighth Kelly) |
| Per-leg aggregate liability | $3,000 across all open combos, netted with maker and taker positions on the same outcome |
| Per-game aggregate | $5,000 |
| Total open combo liability | $25,000 Kalshi and $25,000 Novig (after onboarding) |
| Slate stress test | Loss if every favorite on a slate wins (and separately every over) must stay ≤ $8,000 |

The Kelly arithmetic for the liability cap works as follows. Selling at 15¢ with a true probability of 14¢ makes the quoter's full Kelly fraction (P−q)/P = 6.7% of bankroll as collateral. Quarter Kelly would be about $1,700. **Correlation among shared legs and model error justify one-eighth Kelly at most**, which is the $1,000 cap.

The last-look window is 1 second for Kalshi high-volatility markets, followed by a 15-second execution timer during which neither side can back out ([CFTC rule filing](https://www.cftc.gov/sites/default/files/filings/orgrules/26/01/rules01302638537.pdf)). Novig's last look is also 1 second. Test the exact flow in Kalshi's demo environment, because Kalshi's docs disagree about which party confirms ([Kalshi accept endpoint](https://docs.kalshi.com/api-reference/communications/accept-rfq-quote.md)).

Settlement rules for void and did-not-play legs are inconsistently described ([OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-parlay-leg-voids)). Exclude player props from phase 1 for that reason as well.

## Stale-price taking validates the fair value; arbitrage is pocket money

### The taker as a test of fair value

Pregame taking against de-vigged Pinnacle has the strongest evidence. Betting best-market odds that beat Pinnacle's pre-close fair value returned about **3.6% realized against 3.8% expected over 14 European football seasons** ([Networked substack](https://networked.substack.com/p/a-view-from-the-pinnacle)). Kaunitz and co-authors beat bookmakers with consensus pricing until their accounts were limited ([MIT Technology Review](https://www.technologyreview.com/2017/10/19/67760/the-secret-betting-strategy-that-beats-online-bookmakers/)). Exchanges do not limit winners.

Its role in this plan is bigger than its profit. **Every strategy here prices off the same de-vigged Pinnacle number**, and the taker's closing-line value (CLV) is the cheapest, fastest test of whether that number is right. If taker fills show no CLV, neither the maker nor the parlay quoter has an edge either.

The settings hold up under scrutiny:
- **Edge threshold:** the 2.5% minimum edge is sound now that Novig pregame is fee-free.
- **Sizing:** quarter Kelly at a 2.5% edge on an even-money price is (0.5125 − 0.50)/0.50 × 0.25 ≈ 0.6% of bankroll, about $625.
- **Edge ceiling:** reject edges above about 6–8% as probable data errors ([OddsIndex](https://oddsindex.com/guides/expected-value-calculator)).
- **Who moved first:** take only when Pinnacle has been stable and the exchange price is the one that is off. Vendor claims put retail odds feeds at 3–10 seconds of staleness ([SharpAPI](https://sharpapi.io/learn/how-live-is-real-time-odds-data)), and TheRundown's own lag is unmeasured.

CLV is only a noisy predictor of profit ([Whelan](https://www.karlwhelan.com/?p=2595)), so demand statistical significance before scaling. The capacity math from the earlier analysis still applies: about 5 fills a day at about $600, with 1–2% realized edge, yields roughly $9–18k a year.

### Locked arbitrage

Locked arbitrage benefits most from the fee clarification. A Novig-taker plus Kalshi-taker pair now costs only Kalshi's 1.75¢ at 50¢.

The windows are short. Vendor esports data puts the median pre-match life of an exchange-leg arb at about 10 seconds, with a 90th percentile of about 177 seconds ([dev.to/dozor](https://dev.to/dozor/polymarket-kalshi-arbitrage-vs-sportsbooks-30-days-of-esports-data-1dc2)). Liquid NFL exchange-to-exchange gaps run 1–3¢.

Settlement mismatch is the hidden risk. Kalshi settles ties at 50¢ per side and settles long postponements at a "fair" price rather than voiding ([OddsShopper](https://www.oddsshopper.com/articles/prediction-markets/kalshi-postponed-game-rules)). A 1¢ lock can lose tens of cents on an MLB rainout if Novig voids instead. The rules for running it:
- Keep it opportunistic, at a minimum net lock of 1¢.
- Trade only market types whose tie, postponement and void rules are verified on both venues.
- Stage legs at $1,000 per leg, and unwind if the second leg is not filled within 5 seconds ([trevorlasn](https://www.trevorlasn.com/blog/how-prediction-market-polymarket-kalshi-arbitrage-works)).

Expect a few hundred dollars a month.

## Realistic income tops out near $6–7k a month at this bankroll

### Scenario estimates

The table combines the capacity arithmetic above. Every number is an assumption-driven estimate at full caps after gates are passed, not evidence. **Vendor claims of 12–48% a month for $50k reward farmers are marketing, not data** ([startpolymarket](https://startpolymarket.com/strategies/reward-farming/)). No audited small-operator sports market-making record exists.

| Strategy (capital) | Pessimistic | Base | Optimistic | Key assumption |
|---|---|---|---|---|
| Stale-price taker ($20k, shared) | −$1.0k | +$1.5k | +$3.0k | 1–2% realized edge on about $75k/month of stake |
| Locked arbitrage (shared) | $0 | +$0.3k | +$1.0k | Depth at the gap price |
| Novig pregame maker ($20k) | −$3.0k | +$1.5k | +$5.0k | 0.25–1¢ net per contract after adverse selection |
| Kalshi exempt combos ($25k collateral) | −$2.0k | +$2.5k | +$6.0k | 1–1.5¢ net per contract, about 1-day settlement |
| Novig RFQ ($30k deposit, overlaps maker) | −$1.0k | +$1.0k | +$4.0k | Unmeasured COMBO volume and competition |
| Data and infrastructure | −$0.5k | −$0.5k | −$0.5k | TheRundown about $4.8k a year plus hosting |
| **Total, pre-tax** | **≈ −$7.5k** | **≈ +$6.3k** | **≈ +$18.5k** | |

### What the table means

The base case is about $6k a month, before a tax drag that could be severe (see below). The monthly standard deviation of the combined book is roughly $11–13k. That figure combines taker variance of about $6.6k, maker variance of about $5k with disciplined net inventory, and combo variance of $6–10k after correlation. **Even in the base case, about one month in three loses money.**

That is why kill criteria must rest on edge statistics rather than monthly P&L. Ten good months can look identical to ten lucky ones.

The optimistic column reaches $15–20k only if every engine lands at its optimistic end simultaneously. The two engines that scale, makers and combos, are collateral-bound. **A durable $15–20k a month needs roughly $200–300k of deployed capital and proven edges, not a better algorithm on $100k.**

### Tax can turn a winning book into a losing one

If event contracts are treated as wagering, the 2026 tax law (OBBBA) caps the deduction for wagering losses at 90% of losses, deductible only against winnings and only when itemizing ([Tax Foundation](https://taxfoundation.org/blog/gambling-losses-tax-big-beautiful-bill/); [KPMG](https://kpmg.com/kpmg-us/content/dam/kpmg/pdf/2025/gambling-losses-under-one-big-beautiful-bill.pdf)). A parlay quoter and a maker generate enormous gross losses relative to net profit.

Take a book that nets $75k a year while losing $1M gross on losing positions. It would owe tax on an extra $100k of income it never received. Capital treatment and a §475(f) election are possible alternatives, but neither is settled. Section 1256 treatment is considered aggressive ([Seward & Kissel](https://www.sewkis.com/publications/twelve-fifty-kicks-prediction-markets-1256-contracts-and-other-tax-issues-to-consider/); [ABA Tax Times](https://www.americanbar.org/groups/taxation/resources/tax-times/2026/prediction-market-contracts-involve-financial-instuments-taxation/)). No IRS guidance exists ([TraderTax](https://www.tradertax.net/prediction-market-taxes)).

**A CPA opinion on characterization is therefore a gate before scaling, not a year-end chore.** It determines whether high-turnover liquidity provision is viable at all.

## Six gates decide what scales, starting this month

### Allocation

The bankroll splits by venue:
- **Novig: $50k.** That meets the $30k RFQ minimum and covers maker, taker and arbitrage legs.
- **Kalshi: $35k.** It covers combo collateral, taker and arbitrage legs.
- **Off-venue reserve: $15k.** It covers tax estimates and liquidity.
- **ProphetX and Polymarket US: none.** ProphetX stays a price feed only, and Polymarket US stays watch-only.

### Order of work

The work is ordered by evidence quality per unit of downside:
1. Validate fair value with the taker.
2. Shadow-quote combos while canarying the Novig maker.
3. Go live on exempt Kalshi combos.
4. Onboard to Novig RFQ only if its volume justifies the $30k deposit.

Each gate needs its metric to hold at the next size step before the step is taken.

| Gate (target dates) | Experiment | Pass | Fail → action |
|---|---|---|---|
| G0 Verify (Oct 9–22) | Written answers from Novig on its tie, postponement and void rules and on how a forced NY geofence would treat open positions; wire HTTP 451 to exit mode; Kalshi `fee_type` per series; CPA characterization memo; measure TheRundown lag against a second Pinnacle source; record Kalshi LIP pools for 7 days | All answered; feed lag ≤5s at the 95th percentile | Rules unconfirmed → no locked pairs in that market type; lag >5s → widen cancel thresholds or buy a faster feed before any maker work |
| G1 Shadow (Oct 9–Nov 5) | All modules in shadow on live data; hypothetical maker fills from the Novig trade tape; shadow combo quotes; size Novig COMBO daily volume from data.novig.com | Shadow maker 5-minute markout > 0 at 2¢ half-spread; Novig combo stake ≥ $250k/day | Negative markout → maker paused; thin combos → skip Novig RFQ |
| G2 Taker canary (Oct 23–Dec 3) | $10–$100 live taker fills | ≥300 fills, mean CLV against Pinnacle close ≥ +1.0% with t ≥ 2 | CLV ≤ 0 → stop all strategies; the fair value itself is broken |
| G3 Maker canary (Nov 1–Dec 15) | Novig pregame, $10–$100 clips | ≥300 fills, net 5-minute markout ≥ +0.3¢/contract with 95% lower bound > −0.2¢; picked-off share < 25% | Fail twice at progressively wider spreads → retire the maker |
| G4 Combo live (Nov 15–Jan 15) | Kalshi exempt NFL combos, $50–$250 liability | ≥500 wins, combo CLV against leg closes ≥ +4% of stake; realized loss within 2 standard errors of model | Combo CLV < +2% → raise margin once, then retire |
| G5 Scale and decide (Jan 1–Feb 15, 2027) | Step caps 25% → 50% → 100% | Run-rate expected value at full caps ≥ $10k/month | $5–10k → keep as side income; < $5k → stop and redeploy the capital |

The Jan 1, 2027 checkpoint coincides with the end of Kalshi's LIP and its prop-combo program. It falls before the Super Bowl and before any Supreme Court ruling on sports contracts.

### Kill criteria

There are two layers of kill criteria.

**Edge-based kills come first.** The edge-metric fail conditions in the gate table apply on a rolling basis after scaling:
- Maker 200-fill markout negative.
- Taker CLV ≤ 0 over 300 bets.
- Combo CLV below +2% over 300 wins.

**Loss-based stops run alongside them as circuit breakers, not as edge judgments.**
- **Daily:** −$2,000 halts trading for the day, which the engine already does.
- **Weekly:** −$5,000 halts everything until reviewed.
- **Monthly:** −$10,000 forces a two-week stand-down and a review.
- **Drawdown:** −$15,000 from peak forces a full re-evaluation of the business case.

## Defense works in layers, outermost first

### Legal and venue access

The outermost layer is legal access. New York is the most aggressive state against these venues:
- It sued Kalshi on 2026-07-31, with a reported ask of about $36B ([CNBC](https://www.cnbc.com/2026/07/31/new-york-sues-kalshi-claims-it-is-illegal-gambling-operation.html)).
- It sued Polymarket US on 2026-09-24.
- Kalshi lost its injunction bids in S.D.N.Y. and the Second Circuit ([Covers](https://www.covers.com/industry/federal-judge-denies-kalshi-emergency-injunction-request-in-new-york-july-28-2026)).

The circuit split now runs 1–2 against Kalshi: the Third Circuit sided with Kalshi, while the Sixth and Ninth sided with the states. A Supreme Court ruling is unlikely before June 2027 ([CoinDesk](https://www.coindesk.com/policy/2026/09/25/another-appeals-court-rules-against-prediction-market-provider-kalshi-says-sports-contracts-are-subject-to-state-regulations); [Yogonet](https://www.yogonet.com/international/news/2026/10/08/126758-gaming-regulators-urge-us-supreme-court-to-hear-kalshi-case-decision-could-come-by-june-2027)).

Precedent suggests a geofence with a grace period rather than confiscation. Massachusetts gave 30 days and did not force residents to liquidate positions ([SBC Americas](https://sbcamericas.com/2026/01/20/massachusetts-kalshi-injunction/)). The state frames users as victims owed restitution ([NY AG](https://ag.ny.gov/press-release/2026/governor-hochul-and-attorney-general-james-announce-new-york-has-sued-kalshi)). Mitigations:
- Hold only same-week positions and no futures.
- Keep no more than $50k on any venue.
- Sweep profits to the bank weekly.
- Watch the dockets, especially Novig's own S.D.N.Y. suit (Novig is live for the trader today, but that case could end in a forced geofence), plus NY v. Kalshi and the Second Circuit appeal.
- Build an "exit mode", triggered by an HTTP 451 or a geofence notice, that stops new risk and lets positions run off.

### Counterparty and operations

Funds sit with each venue's clearinghouse: Kalshi Klear under Part 22 segregation ([CFTC filing](https://www.cftc.gov/media/13696/KKL%20-%2004-06-2026%20-%20Binary%20Option%20Event%20Contracts/download)), and Bitnomial for Novig ([Camuso CPA](https://camusocpa.com/novig-taxes/)). Insolvency of an event-contract venue is untested, which is another reason to sweep balances.

Operational protections:
- Reconcile the bot's order state against the exchange after every reconnect ([casatrick](https://casatrick.substack.com/p/polymarket-trading-bot-state-reconciliation)).
- Run a dead-man switch that cancels everything on a lost heartbeat.
- Recheck fills against venue statements daily.

### Model and correlation

The model layer protects against bad fair values:
- Use two de-vig methods, and quote only when they agree within 1¢.
- Reject edges above 6–8%.
- Check fill-before-move for each fill.

The correlation layer nets taker, maker and combo exposure into one risk view per outcome, team and slate, so a "favorites all win" Sunday cannot breach $8k across all engines combined.

### Tax

The tax layer completes the stack:
- Keep exportable logs for every venue.
- Make quarterly estimated payments.
- Hold a CPA-documented characterization decision before Gate 5.

## Conclusion

The research reframes the trader's question from "which strategy is best" to "which constraint binds first." For pregame making, the binding constraint is toxicity at the quoted width. For parlays, it is collateral and the winner's curse. For taking and arbitrage, it is capacity. Across all of them, it is the unmeasured accuracy of a single Pinnacle-derived fair value. That shared dependency is the plan's real structure. A few hundred taker fills cheaply test the asset every engine relies on, so the taker's CLV is a gate for the whole business, not a side strategy.

Two non-obvious levers stand out. The first is Kalshi's maker-fee exemption for uncorrelated NFL parlays. It points a new quoter at the one combo segment where its model error is smallest and its fee is zero. The second is the tax characterization. Under wagering treatment it can turn a profitable high-turnover book into a losing one, so it should be settled before scaling. At $100k the honest ceiling is a solid side income. $15–20k a month needs proven edges plus two to three times the capital. Gates G2 through G4 will show by mid-January 2027 whether those edges exist, at a cost of a few thousand dollars in canary risk.
