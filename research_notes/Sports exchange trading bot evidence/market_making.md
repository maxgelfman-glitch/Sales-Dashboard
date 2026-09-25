# Market Making on Sports Prediction Markets & Betting Exchanges (Kalshi, Novig, ProphetX, Polymarket, Sporttrade, Betfair), 2024-2026

Method note: WebFetch was blocked by the network egress proxy for most primary domains (help.kalshi.com, igamingbusiness.com, prospect.org), so the findings below come from search-engine extracts of the cited pages, not full-page reads. Where a claim comes from an SEO/affiliate or tool-vendor site (startpolymarket.com, botforkalshi.com, sacra.com, thecompound.news, oddsassist.com, etc.), it is marked **[secondary/marketing]**. Academic papers are marked **[data]** and forum or blog posts **[anecdote]**. Research date: 2026-09-24.

---

## 1. Who the professional market makers are, and how that affects small makers

### Takeaway
Kalshi sports liquidity is dominated by institutional firms: Susquehanna (SIG) since April 2024, Jump, and Kalshi's own affiliate Kalshi Trading LLC (<6% of sports making volume), with DRW, IMC, Wintermute and others building desks. Novig, Polymarket and Crypto.com also have or are hiring affiliated trading arms. ProphetX and Sporttrade rely on institutional or house-contracted makers. A $100k independent bot competes on price and speed against firms that have fee deals, higher rate limits, sealed incentive payments and better models. On the main pregame lines (NFL/NBA sides and totals), a small maker is mostly a price-taker behind these firms in the queue.

### Cited Findings
- SIG announced a partnership with Kalshi in April 2024 and became the first institutional market maker to launch a desk dedicated to event contracts. SIG started a dedicated prediction-markets desk in 2023. — [Kalshi blog "Getting liquid with SIG"](https://kalshi.com/blog/article/kalshi-kit-liquidity-sig-market-makers); [SIG Predictions](https://sig.com/predictions/); [Kalshi News](https://news.kalshi.com/p/liquid-prediction-markets-are-finally-here)
- SIG also runs Nellie Analytics, a Dublin quant sports-betting unit started in 2017 that focuses mainly on in-game wagering. — [efinancialcareers](https://www.efinancialcareers.com/news/quants-sports-betting); [Bloomberg 2017](https://www.bloomberg.com/news/articles/2017-10-30/sports-gambling-is-quant-trading-firm-s-way-to-beat-market-odds)
- Jump Trading was already making markets on Kalshi before reported talks to take equity stakes in Kalshi and Polymarket ("equity for liquidity"). — [DeFi Rate](https://defirate.com/news/equity-for-liquidity-jump-trading-set-to-take-stakes-in-kalshi-and-polymarket/)
- DRW, Wintermute and IMC are building dedicated prediction-market desks. Coverage describes their focus as short-term pricing inefficiencies, arbitrage and microstructure rather than forecasting outcomes. — [CoinDesk, Jun 2026](https://www.coindesk.com/business/2026/06/06/a-massive-hiring-wave-reveals-trading-firms-are-no-longer-viewing-polymarket-as-a-niche-betting-tool); [Tradermath](https://www.tradermath.org/articles/prediction-markets-trading-at-quant-firms)
- CNBC (Aug 2026) reports that hedge funds are about to move heavily into prediction markets. — [CNBC](https://www.cnbc.com/2026/08/19/hedge-funds-are-about-to-jump-in-big-to-prediction-markets.html)
- Kalshi affiliate Kalshi Trading LLC:
  - Co-founder Luana Lopes Lara said it was "not profitable" on sports and accounted for "less than 6% of the making volume" in sports in November (2025).
  - Sportico-derived math puts that at about $310M of sports trades in the month, which implies about $5B+ of total sports making volume.
  - DraftKings co-founder Matt Kalish accused Kalshi of volume padded by its in-house MM.
  - [InGame](https://www.ingame.com/kalshi-in-house-trading-arm-not-profitable/); [Yahoo Finance](https://finance.yahoo.com/news/kalshi-co-founder-says-house-174507266.html); [Sportico](https://www.sportico.com/business/sports-betting/2025/kalshi-trading-exchange-peer-house-1234870465/); [FairGambling](https://www.fairgambling.com/news/kalish-kalshi-volume-market-maker-allegation)
- A class action (Nov 2025) alleges that users were effectively betting against Kalshi Trading and hedge-fund partners like SIG, which "bet against consumers when their bets stray from Kalshi's internal projected odds". It also alleges that about 90% of Kalshi's September intake (about $2B) was sports. SIG is named in state-court filings. — [iGaming Business](https://igamingbusiness.com/sports-betting/class-action-suit-against-kalshi-market-makers/); [Front Office Sports](https://frontofficesports.com/article/kalshi-hit-with-nationwide-class-action-over-illegal-sports-betting/); [InGame on SIG](https://www.ingame.com/market-maker-susquehanna-named-court-filings/) **[legal allegation, not established fact]**
- The CFTC says it knows of "at least six" exchanges that permit affiliated principal trading firms.
  - Novig's affiliate Manhattan Athletic Group trades on its exchange behind an information barrier.
  - Crypto.com and Polymarket posted job listings for their own affiliated trading arms.
  - Sportico says it is not known how many of these desks are profitable.
  - [Sportico, "Oddsmaking Battle"](https://www.sportico.com/business/sports-betting/2026/prediction-market-maker-affiliate-odds-1234884140/); [Sportico on CFTC affiliate rule](https://www.sportico.com/business/sports-betting/2026/prediction-market-affiliate-rule-cftc-conflicts-trading-1234941260/)
- ProphetX works with third-party institutional market makers and says it has no affiliated trading arm ("We won't be trading against our customers"). It publicly opposes affiliated arms. — [Sportico](https://www.sportico.com/business/sports-betting/2026/prophetx-prediction-market-affiliated-trading-arms-1234909865/); [ProphetX market-makers page](https://www.prophetx.co/lobby/market-makers/)
- Sporttrade uses its own market makers, who are "obligated to set fair offers" so that pricing stays competitive in thin markets. Its technology strategy targets institutional MMs as well as retail. — [BettingUSA review](https://www.bettingusa.com/sports/reviews/sporttrade/) **[secondary]**
- Novig's founder has said the long-term plan is to charge professional market makers and liquidity firms, not casual bettors, for access. — [OddsJam](https://oddsjam.com/betting-education/commission-free-sports-betting-exchange) / [AlleyWatch on the $75M raise](https://www.alleywatch.com/2026/03/novig-sports-prediction-market-peer-to-peer-exchange-commission-free-trading-jacob-fortinsky/) **[secondary]**
- Spreads:
  - A secondary source claims SIG's making reduces spreads to 0.5-1.5% on major Kalshi contracts, versus 3-5% on Polymarket.
  - Another source says prediction-market median spreads are about 200-500 bps overall.
  - [The Compound](https://thecompound.news/prediction-markets-fire-susquehanna-kalshi-2026/) **[secondary, methodology unknown]**; [navnoorbawaresearch](https://www.navnoorbawaresearch.com/p/market-making-in-sports-betting-how) **[secondary]**
- Scale: Kalshi monthly volume grew from $226M (Dec 2024) to $6.6B (Dec 2025) and $29.2B (Jun 2026). Sports were about 80% of fee-generating volume in June 2026. — [Sacra](https://sacra.com/c/kalshi/) **[secondary]**

### Inferences
- On the top pregame markets (NFL/NBA moneylines, spreads, totals), several institutions quote a penny or two wide in size. A small maker is either behind them in the price-time queue or, when first in the queue, disproportionately filled when the institutions have already pulled or moved, which is adverse selection.
- The realistic niche for a $100k bot is second-tier markets: player props, smaller leagues, early-week lines, and alt spreads/totals. Institutions quote these less tightly, and incentive programs (section 2) target them. Adverse selection per contract is also higher there (section 3).
- Kalshi's own affiliate is reportedly unprofitable on sports. That is weak but notable evidence that pure sports making is not easy money even with inside-the-exchange advantages. It could also reflect an intentional loss-leader liquidity role.

### Gaps
- There are no public per-firm market-share figures for SIG, Jump and others on Kalshi sports, and no disclosed quoted spreads by firm.
- Novig's and ProphetX's specific named MM firms were not found in public sources.
- I found no reliable data on Sporttrade's MM economics.

---

## 2. Market-maker and liquidity incentive programs (fees, rebates, terms)

### Takeaway
Makers on the US sports venues mostly pay nothing, and several venues pay makers:
- **Kalshi:** makers pay no fee on most markets and a 0.0175·P·(1-P) fee on designated ones. Kalshi runs an open Liquidity Incentive Program (cap $1,000 per market per day) and a sealed Market Maker / Liquidity Provider program (up to $50,000 per series per week, set by reverse auction, only for firms with a Market Maker Agreement).
- **Polymarket:** makers pay zero, earn a share of taker fees (sports share reported at 15-25%, sources conflict), and can earn a separate daily liquidity reward.
- **Novig:** pregame is free for everyone. Makers earn a credit of 50% of the taker fee on live fills.
- **ProphetX and Sporttrade:** about 2% commission on net winnings.

### Cited Findings
**Kalshi fees**
- Taker fee = roundup(M × 0.07 × C × P × (1-P)). Maker fee = roundup(M × 0.0175 × C × P × (1-P)) only on designated markets. The default maker multiplier is zero. At 50¢ the taker fee is about 1.75¢ per contract, and the maker fee where charged is about 0.44¢. — [Kalshi fee schedule](https://kalshi.com/fee-schedule); [Kalshi Help: Fees](https://help.kalshi.com/en/articles/13823805-fees); [botforkalshi](https://www.botforkalshi.com/blog/kalshi-fees-explained) **[secondary on arithmetic]**
- The July 2026 schedule lists many sports series (pro and college football, basketball, baseball, hockey, golf, tennis, World Cup) with taker multiplier 1. — [marketmath](https://marketmath.io/platforms/kalshi) **[secondary]**

**Kalshi Liquidity Incentive Program (open to all non-MM members)**
- It pays for resting orders that improve liquidity, even if they are never filled.
- Each program has its own market, start and end time and reward pool, plus a Target Size (100-20,000 contracts of depth per side required for a snapshot to count) and a Discount Factor (up to 1.00) that discounts orders priced away from the reference price.
- Kalshi can change or end programs at any time. Governing terms are posted at kalshi.com/regulatory/notices.
- [Kalshi Help: LIP](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program); [Kalshi Incentives page](https://kalshi.com/incentives)
- The open tier is capped at $1,000 per market per calendar day. — [Navnoor Bawa, "Kalshi Publishes One Liquidity Subsidy and Seals the Other"](https://www.navnoorbawaresearch.com/p/kalshi-publishes-one-liquidity-subsidy) / [X version](https://x.com/navnoorquant/article/2088369227283751256) **[analyst commentary citing Kalshi filings]**
- The LIP CFTC filing was framed as possibly enabling a "garage-band market maker" class. That commentary notes Polymarket's program had already spawned anonymous full-time quoting teams. — [fiftycentdollars/ufoholdings Substack](https://fiftycentdollars.substack.com/p/kalshis-new-liquidity-incentives) **[opinion]**
- Kalshi also runs a Volume Incentive Program. — [Kalshi Help](https://help.kalshi.com/en/articles/13823850-what-is-the-kalshi-volume-incentive-program)
- Critics argue the incentive programs subsidise volume. — [ZeroHedge, "Kalshi's $5,499 Question"](https://www.zerohedge.com/crypto/kalshis-5499-question-wash-trading-or-subsidized-volume-machine) **[opinion]**

**Kalshi Market Maker / Liquidity Provider Program (sealed)**
- Only members who have signed a Market Maker Agreement are eligible.
- A member can become a "Designated Liquidity Provider" for "Incentivized Series" by winning a reverse auction, bidding the minimum Incentive Period Reward it will accept to meet quoting requirements.
- Caps are reported at $50,000 per series per week. The program targets new or thin markets.
- [Kalshi Help: Liquidity Provider Program](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program); [Navnoor Bawa](https://www.navnoorbawaresearch.com/p/kalshi-publishes-one-liquidity-subsidy)
- The Fee Rebate Program filed with the CFTC gives market makers reduced fees and adjusted position limits in exchange for quoting and volume obligations. Rebate rates and caps are "communicated in writing to Program Participants", can change "with seven (7) days' notice", and applicants may be waitlisted "indefinitely". — [CFTC filing: KalshiEX Fee Rebate Program](https://www.cftc.gov/filings/orgrules/rules01132513688.pdf); [Kalshi Help: How to become a market maker](https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi)
- Kalshi approves MM status after reviewing financial resources, trading experience and reputation. Obligations include uptime and two-sided quotes within tight spreads. — [Kalshi Help](https://help.kalshi.com/en/articles/13823819-how-to-become-a-market-maker-on-kalshi); [kalshibacktest](https://kalshibacktest.com/resources/what-is-kalshi-market-maker) **[secondary]**

**Polymarket**
- Fee Structure V2 took effect 2026-03-30:
  - Takers pay a rate-based fee: crypto 0.07, sports 0.03, finance/politics/tech 0.04, other 0.05.
  - Makers pay zero and receive daily rebates of 25% of taker fees in most categories, 20% in crypto and 15% in sports.
  - Fees rolled out on select sports markets in February 2026.
  - [Polymarket Help: Maker Rebates Program](https://help.polymarket.com/en/articles/13364471-maker-rebates-program); [startpolymarket](https://startpolymarket.com/learn/polymarket-fees/) **[secondary]**
- **Conflicts on sports terms:**
  - One source says sports makers initially got a 25% rebate.
  - Another says that "as of July 2026 sports carries a 0.05 rate and the smallest rebate share (15%)".
  - iGaming Business reports that Polymarket "adjusted" (raised) sports fees in 2026.
  - [iGaming Business](https://igamingbusiness.com/prediction-markets/polymarket-sports-fee-hike-2026/); [startpolymarket rebates](https://startpolymarket.com/learn/polymarket-rebates-rewards/) **[treat the exact current sports rate as unverified]**
- A separate Liquidity Rewards program pays daily at midnight UTC for limit orders resting near the midpoint. It uses a quadratic score by distance from mid, sampled each minute. — [startpolymarket reward farming](https://startpolymarket.com/strategies/reward-farming/) **[secondary]**; [spfunctions/polymarket-sports-mm README](https://github.com/spfunctions/polymarket-sports-mm)
- One GitHub README claims "Polymarket pays $5M+/month to market makers who post resting limit orders on sports markets". — [spfunctions/polymarket-sports-mm](https://github.com/spfunctions/polymarket-sports-mm) **[unverified claim]**
- Polymarket US reportedly pays makers to rest orders, whereas Kalshi's maker side is free by default. — [clawarbs](https://clawarbs.com/blog/kalshi-vs-polymarket-arbitrage/) **[secondary]**

**Novig**
- There is no fee on pregame trades or maker fills.
- The taker fee applies only to live in-game trades and parlays, capped at about $0.0075 per contract.
- The Maker Credit Program rebates 50% of the taker fee on live maker fills.
- [Novig Help: Fees](https://support.novig.com/en/articles/16195057-fees-on-novig); [OddsAssist](https://oddsassist.com/prediction-markets/novig-fees/) **[secondary]**
- Novig raised $75M in 2026 and launched a CFTC-regulated sports-only exchange nationwide on 2026-08-04. — [Fortune](https://fortune.com/2026/02/18/sports-prediction-markets-novig-kalshi-polymarket-cftc-pantera-multicoin/); [Covers](https://www.covers.com/industry/novig-launches-sports-focused-prediction-market-platform-nationwide-aug-4-2026)

**ProphetX and Sporttrade**
- ProphetX charges 2% commission on net winnings per market (1-3% depending on volume). It received CFTC approval shortly before the 2026 World Cup. — [Legal Sports Report](https://www.legalsportsreport.com/prediction-markets/prophetx-promo-code/); [Sportico](https://www.sportico.com/business/sports-betting/2025/prophetx-prediction-market-cftc-sweepstakes-1234876170/)
- Sporttrade charges 2% commission on profitable trades, an effective edge of about 2.4%. It is moving from state licensing toward CFTC approval. — [BettingUSA](https://www.bettingusa.com/sports/reviews/sporttrade/) **[secondary]**

### Inferences
- **Kalshi:** the published LIP is the only incentive a small trader can realistically access. It is capped at $1,000 per market per day and shared pro rata among everyone meeting the target size, so payouts on busy sports markets are diluted by institutional depth. The sealed $50k-per-week tier needs a Market Maker Agreement, a financial review and an auction, which is probably out of reach at $100k.
- **Polymarket:** maker economics are the most explicit. The rebate is a share of taker fees, split pro rata by maker volume, plus rewards. However, the sports rebate share is the lowest category (15%) and has changed several times in 2026, so a model that depends on it is fragile.
- **Novig and Kalshi** charge makers nothing on most pregame markets, so gross spread capture minus adverse selection is the whole P&L. **ProphetX and Sporttrade** take 2% of net winnings, which directly eats thin maker edges.

### Gaps
- I could not see actual current LIP pool sizes for specific sports markets. Kalshi's incentives page is dynamic and was not fetchable.
- There is no public data on typical realized LIP payouts per trader.
- Polymarket's exact current sports taker rate and rebate share conflict across sources.
- I found no published maker program for ProphetX or Sporttrade beyond the commission rate.

---

## 3. Adverse selection in sports markets and how makers manage it

### Takeaway
Academic evidence on Kalshi shows that makers as a group earn positive or less-negative returns than takers. Takers lose about 1.12% per trade (Becker). Takers lose far more than makers on longshots (Bürgi-Deng-Whelan). The maker edge comes largely from biased retail "YES"/longshot flow rather than from avoiding informed flow. Informed traders do pick off slow quotes, and makers widen spreads in response, especially in single-name markets. For sports, the key informed-flow events are injury and lineup news and sportsbook line moves ("steam"). Makers defend by anchoring to sharp sportsbook lines, pulling quotes quickly, and keeping inventory small.

### Cited Findings
- Bartlett & O'Hara (Stanford Law, Apr 2026), covering 41.6M trades across 478,167 Kalshi markets:
  - Informed traders systematically pick off slow quotes, and makers widen spreads in response, more in single-name markets.
  - Effective spreads are "only modestly wider" and makers earn about twice as much per contract, because traders overbet YES (they buy YES about 61% of the time while YES wins about 32% in single-name markets).
  - This behavioral surplus cross-subsidizes adverse selection.
  - [Stanford Law blog](https://law.stanford.edu/2026/04/21/adverse-selection-in-prediction-markets-evidence-from-kalshi/); [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6615739); [InGame summary](https://www.ingame.com/study-kalshi-betting-yes/) **[data]**
- Bürgi, Deng & Whelan, "Makers and Takers: The Economics of the Kalshi Prediction Market" (300k+ contracts):
  - Prices are informative and there is a clear favorite-longshot bias.
  - Takers lose about 32% on average, versus about 10% for makers. These are figures dominated by longshot contracts.
  - High-priced contracts give small positive returns.
  - [UCD WP](https://www.ucd.ie/economics/t4media/WP2025_19.pdf); [SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5502658); [CEPR VoxEU](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market) **[data; note that makers still lose on average in this sample, which is not all sports]**
- Becker, "Microstructure of Wealth Transfer" (72.1M Kalshi trades, $18.26B):
  - Takers earn -1.12% mean excess return per trade and makers +1.12%.
  - Maker returns are nearly identical whether they buy YES or NO, so makers profit from a spread or bias mechanism rather than directional skill.
  - [jbecker.dev](https://www.jbecker.dev/research/prediction-market-microstructure) **[data, independent researcher]**
- A GitHub replication is testing whether the maker/taker asymmetry survived the introduction of maker fees. — [Vladosyna/kalshi-makers-takers-persistence](https://github.com/Vladosyna/kalshi-makers-takers-persistence) **[in progress]**
- A marketing blog cites a Polymarket analysis: excluding high-volume wallets, the rest lost about $131M, while 823 wallets that each staked over $100k netted about +$131M. — [Turbine blog](https://www.turbinefi.com/blog/why-prediction-market-trades-get-picked-off-2026) **[secondary/marketing; original source not verified]**
- A trader who crosses the spread chooses when to execute, while a resting maker does not. That is why takers who cross because they expect a move (informed flow) bias fills against makers. — [BowTiedBettor, "Adverse Selection in Betting Markets"](https://www.blog.bowtiedbettor.com/p/adverse-selection-in-betting-markets) **[practitioner blog]**
- On Betfair, being first in the queue raises fill probability. The risk is that sharper market makers "pick you off" by trading against your quotes. — [mildbyte, project Betfair part 4](https://mildbyte.xyz/blog/project-betfair-part-4/) **[anecdote/practitioner]**
- Sportsbook lines move faster than Kalshi in many markets, so detecting a line move and repricing on Kalshi within seconds is an edge. For a maker that is also the main source of being picked off. Bots can misread lineup rumors versus confirmed injuries. — [botforkalshi strategies guide](https://www.botforkalshi.com/blog/kalshi-trading-strategies-guide) **[secondary/vendor]**
- Kalshi removed athlete-injury markets at the CFTC's request in 2026, which shows how central injury news is to this information asymmetry. — [Sportico](https://www.sportico.com/business/sports-betting/2026/kalshi-injury-betting-nfl-cftc-1234943772/); [CDC Gaming](https://cdcgaming.com/brief/kalshi-removes-athlete-injury-markets-after-cftc-intervention/)
- Kalshi froze insider accounts and opened about 200 insider-trading probes around the $1.5B Super Bowl trading. — [CryptoSlate](https://cryptoslate.com/200-insider-trading-probes-opened-on-kalshi-and-one-quiet-change-could-remake-prediction-markets-overnight/)
- Class-action allegations say Kalshi-affiliated makers trade against users when prices "stray from Kalshi's internal projected odds". In other words, professional makers anchor to a model or fair price. — [iGaming Business](https://igamingbusiness.com/sports-betting/class-action-suit-against-kalshi-market-makers/) **[allegation]**

### Inferences
- The academic results are about makers in aggregate, and that aggregate is dominated by professionals with fast cancel/replace and sharp fair values. They do not show that a slow retail maker earns +1%. The maker edge in these papers also leans heavily on longshot and YES-bias flow. Pregame sports sides and totals near 50¢ show less of that bias than novelty or single-name markets.
- Practical defenses for a small bot:
  - Anchor fair value to a de-vigged sharp book (Pinnacle/Circa-style).
  - Cancel all quotes when the sharp line moves or when injury or lineup windows open, such as NBA injury report deadlines, about 90 minutes before tip, and NFL inactives about 90 minutes before kickoff.
  - Cap inventory per event.
  - Avoid being the only quote at the touch.
- Mechanically, the P&L equals spread captured, plus rebates or rewards, minus adverse-selection losses, minus fees. With 1-2¢ spreads on major lines, a few stale fills on steam moves can erase the spread from many benign fills.

### Gaps
- I found no sports-only split of maker versus taker returns in these papers from the snippets available, so sports-specific maker returns remain unverified.
- There is no published data on typical pregame spread by market type (main line versus props) on Kalshi, Novig or ProphetX.
- There is no quantitative measure of how fast Kalshi prices lag sharp sportsbook moves.

---

## 4. Reported results of individuals running market-making bots

### Takeaway
Credible public P&L from independent sports market-making bots is scarce, and what exists is mostly negative or break-even:
- A documented Betfair MM bot lost money live despite a profitable backtest.
- The most popular open-source Polymarket MM bot warns that it can lose money.
- Kalshi's own affiliate reports losses in sports.

Positive claims come mostly from vendors, affiliates, or reward-farming (not spread capture). I found no verified record of a $100k independent sports MM bot being durably profitable on a US venue.

### Cited Findings
- mildbyte's Betfair market-making bot (UK pre-race horse racing) lost £9.27 over 172 races (395 bets, £1,409 total stakes, never more than £15 at risk). It "consistently lost money, contrary to the backtest", and the author shelved the project. — [mildbyte project Betfair part 7](https://mildbyte.xyz/blog/project-betfair-part-7/); [part 8](https://mildbyte.xyz/blog/project-betfair-part-8/) **[anecdote, detailed; pre-2024 but a canonical reference]**
- The Betfair community forum threads ("Profitable Bots", "Automating Competitive Markets") describe UK pre-race as very competitive and bot-dominated. They report that profitable bots tend to be narrow, low-frequency strategies, e.g. about 10 trades a month with mixed results, rather than broad MM. — [Betangel forum "Profitable Bots"](https://forum.betangel.com/viewtopic.php?t=12623); [Automating Competitive Markets](https://forum.betangel.com/viewtopic.php?t=25751); [Caanberry on pre-race bots](https://caanberry.com/pre-race-price-movements-on-betfair/) **[anecdote]**
- The warproxxx/poly-maker README, the most-cited open-source Polymarket MM bot, says: "Market making on Polymarket is competitive and can lose money. This is a reference implementation and a research harness, not a guaranteed-profitable product." Its quotes stay inside the liquidity-rewards band, and it scores markets by rewards and rebates. — [GitHub poly-maker](https://github.com/warproxxx/poly-maker) **[author disclaimer]**. (Earlier versions of the README reportedly stated more bluntly that it was no longer profitable. I could not verify the current wording beyond the quoted text.)
- Other GitHub bots (spfunctions/polymarket-sports-mm, terrytrl100/polymarket-automated-mm, polymarket-liquidity-rewards-bot) are designed around reward farming, optimizing for the quadratic rewards function in pregame and live sports. None publishes audited P&L. — [spfunctions](https://github.com/spfunctions/polymarket-sports-mm); [terrytrl100](https://github.com/terrytrl100/polymarket-automated-mm); [rewards-bot](https://github.com/polymarket-liquidity-rewards-bot/polymarket-liquidity-rewards-bot)
- A secondary site claims that a maker whose orders fill at flat mid is net positive on rebates alone on Polymarket in 2026. — [startpolymarket market-making](https://startpolymarket.com/strategies/market-making/) **[secondary/marketing]**
- A Medium post claims a "$500 monthly side income" from Polymarket and Kalshi incentives, which is small-scale reward farming. — [Medium, Ezekiel Njuguna](https://medium.com/mountain-movers/turning-polymarket-and-kalshi-into-a-500-monthly-side-income-to-fund-claude-max-perplexity-max-474ab67244d5) **[anecdote, unverified]**
- Kalshi Trading LLC, the exchange's own affiliate, was "not profitable" on sports making in November 2025. — [InGame](https://www.ingame.com/kalshi-in-house-trading-arm-not-profitable/)
- A vendor-run AI bot review from 60 days on Kalshi exists but is not MM-specific. — [PillarLab](https://pillarlabai.com/blog/ai-betting-bot-review-kalshi-60-days/) **[marketing]**

### Inferences
- The available evidence points to incentive capture (Polymarket rewards and rebates, Kalshi LIP) as the realistic income source for small makers, not raw spread capture on major pregame lines. Reward income is at the platform's discretion and has shrunk or changed repeatedly.
- Backtest-to-live degradation (mildbyte) is the classic failure mode, because backtests do not model queue position or adverse selection.

### Gaps
- Reddit threads (r/Kalshi, r/Polymarket, r/algobetting, r/SportsTrading) could not be retrieved, and no specific Reddit P&L reports were found in search results.
- There are no audited or verifiable P&L records for any independent sports MM bot on Kalshi, Novig or ProphetX.

---

## 5. Hedged / cross-venue market making

### Takeaway
Cross-venue hedging (rest on venue A, hedge on venue B or a sportsbook when filled) is widely discussed as "Kalshi vs Polymarket arbitrage", and prime-broker tools exist for it. I found no credible public performance evidence for it. The pitfalls follow directly from the other sections: when a resting order fills, the hedge venue has often already moved, so you are adversely selected on both legs. Fees on the hedge leg, resolution-rule mismatches, and capital split across venues also cut into the edge.

### Cited Findings
- Kalshi-vs-Polymarket arbitrage is marketed as an accessible cross-venue strategy for individuals. Fee tiers matter, and "at the 1% tier most cross-venue arbs become profitable". Kalshi's maker side is free by default, while Polymarket US pays makers. — [Claw Arbs](https://clawarbs.com/blog/kalshi-vs-polymarket-arbitrage/) **[vendor/marketing]**
- River Markets offers a prime-brokerage-style single terminal and API across prediction venues for funds and professional traders. This is infrastructure aimed at institutions doing multi-venue trading. — [River Markets](https://www.rivermarkets.com/)
- Oddpool sells historical order book data for Kalshi and Polymarket and cross-venue odds comparison. — [Oddpool](https://www.oddpool.com/institutional)
- Sportsbook lines move before Kalshi's in many markets. — [botforkalshi](https://www.botforkalshi.com/blog/kalshi-trading-strategies-guide) **[vendor]**

### Inferences
- The fill on a resting order is conditioned on someone wanting to trade at your price. That is most likely right after the reference market (sharp book or the other exchange) has moved, so hedging at the pre-move price usually fails.
- A hedged approach works best when the hedge venue is deep and slow-moving relative to where you rest the order. For example, rest slightly off a de-vigged sharp price on a thin exchange market and hedge on a liquid venue only when the fill is clearly off-market.
- Account limits at US sportsbooks make them an unreliable hedge venue for a persistent strategy.
- Resolution-rule differences between Kalshi, Polymarket and Novig (overtime, postponements, void rules) are a known cross-venue risk. I did not find a sourced incident.

### Gaps
- There are no documented results, positive or negative, from traders running rest-and-hedge MM across these venues.

---

## 6. Queue position, order-rate limits and API constraints

### Takeaway
Kalshi's API uses token-bucket rate limits by tier. Basic accounts get about 10 orders per second, and tiers above Advanced are earned through volume or assigned by Kalshi. Top tiers allow hundreds of orders per second. Venues are price-time priority, so a small bot is structurally behind institutions in the queue and in cancel speed. On Betfair, queue position is recognized as the key factor in fill probability and adverse selection.

### Cited Findings
- Kalshi read/write token budgets per second by tier:

  | Tier | Read / write tokens per second |
  |---|---|
  | Basic | 200 / 100 |
  | Advanced | 300 / 300 |
  | Expert | 600 / 600 |
  | Premier | 1,000 / 1,000 |
  | Paragon | 2,000 / 2,000 |
  | Prime | 4,000 / 4,000 |
  | Prestige | 10,000 / 8,000 |

  - An order costs 10 write tokens, and a batch of N orders costs N×10. That gives about 10 orders per second for Basic, 30 for Advanced and 100 for Premier.
  - Basic comes with signup. Advanced is self-service through an API endpoint. Expert and above are earned automatically from trading volume or assigned by Kalshi.
  - There is no penalty or cooldown for hitting the limit; requests succeed once tokens refill.
  - [Kalshi API docs: Rate Limits and Tiers](https://docs.kalshi.com/getting_started/rate_limits); [botforkalshi](https://www.botforkalshi.com/blog/kalshi-api-rate-limits) **[secondary on derived numbers]**
- Kalshi market makers get "reduced fees and certain adjusted position limits" under the Fee Rebate Program. — [CFTC filing](https://www.cftc.gov/filings/orgrules/rules01132513688.pdf)
- A resting limit order joins the queue behind orders already at that price. — [OddsShopper, Kalshi order types](https://www.oddsshopper.com/articles/prediction-markets/kalshi-order-types) **[secondary]**
- On Betfair, being at the front of the queue raises fill probability but exposes you to sharper makers picking you off. — [mildbyte part 4](https://mildbyte.xyz/blog/project-betfair-part-4/)
- A developer guide covers common Kalshi API problems for bot builders. — [AgentBets](https://agentbets.ai/guides/kalshi-api-top-10-problems/) **[secondary]**

### Inferences
- About 30 orders per second on the Advanced tier is enough for a pregame bot quoting dozens of markets, as long as it batches and reprices only on fair-value changes. The main problem is latency relative to co-located or assigned-tier institutions when news breaks, not throughput.
- Because the higher tiers are volume-gated, a small maker starts at a structural cancel-speed disadvantage exactly when adverse selection matters most.

### Gaps
- No published rate limits were found for Novig, ProphetX or Sporttrade APIs, and it is unclear whether retail API access exists for all three.
- The Polymarket CLOB rate limits were not retrieved.
- There is no public latency data (co-location, WebSocket delays) for Kalshi.

---

## 7. Venue access for a New York user, and in-play notes

### Takeaway
Kalshi, Novig (nationwide CFTC exchange launched August 2026) and ProphetX (CFTC-approved 2026) are federally accessible. Polymarket US removed its waitlist in May 2026. Promo terms exclude AZ, IL, MA, MD, MI, MT, NJ, NV and OH, but not NY, so eligible NY users can access it. NY regulators have said sports-focused prediction markets may conflict with state law, which is an ongoing legal risk. In-play markets carry much higher adverse-selection risk from broadcast latency and courtsiders, and Novig charges takers only in-play (with a 50% maker credit), which reflects that.

### Cited Findings
- Polymarket US removed its waitlist in May 2026 and is live on iOS and Android, with web in beta. NY users can access it. NY regulators say sports-focused prediction markets "may still run afoul of New York law". The launch was delayed after a rocky invite period. — [SportsHandle NY](https://sportshandle.com/polymarket-promo-code/new-york/); [TheLines NY](https://www.thelines.com/prediction-markets/polymarket/new-york/); [Sportico on the US launch](https://www.sportico.com/business/sports-betting/2026/polymarket-united-states-launch-invite-waitlist-delay-1234879944/) **[secondary on current NY status]**
- Novig launched a nationwide sports-focused prediction market on 2026-08-04. — [Covers](https://www.covers.com/industry/novig-launches-sports-focused-prediction-market-platform-nationwide-aug-4-2026)
- Novig charges taker fees only on live and parlay trades, and pregame is free. — [Novig Help](https://support.novig.com/en/articles/16195057-fees-on-novig)

### Inferences
- A pregame-only strategy on Novig and Kalshi keeps fees at zero for makers. In-play making is where institutions like SIG's Nellie specialize, and a small bot should avoid it.

### Gaps
- The legal status of NY residents on each venue could change. I found no NY enforcement action against individual traders.
