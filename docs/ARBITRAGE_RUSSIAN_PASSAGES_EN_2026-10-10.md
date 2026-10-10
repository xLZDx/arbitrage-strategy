# Arbitrage Research — English Translation of the Russian Passages

**Original:** [`arbitrage.txt`](../arbitrage.txt), Git blob `41bcce07db11edc2f1256ea723d3f5ed4d9253c4` on the original `main` tree inspected October 10, 2026.  
**Scope:** Translate the Russian commentary, headings, code documentation and quoted suggestions in the historical research file. The rest of `arbitrage.txt` already contains English proposals/code and is not reproduced in full.  
**Authority:** Documentary translation only. No strategy has been implemented, backtested, deployed, or authorized for live execution by this translation. The existing [architecture](../core/ARCHITECTURE.md), [plan](../core/PLAN.md) and [risks](../core/RISK.md) must be consulted for actual project scope.

The headings below identify the **original text's line ranges**, so reviewers can compare the complete historical statements rather than relying on a paraphrase. First-person assertions in the original are rendered as dated research claims, not as newly verified project facts.

## 1. Weighted Multi-Level OBI — Python example (source lines 227–274)

### Original explanatory introduction — translated

"Certainly. As a QA Automation Lead, you will appreciate the cleanliness of this approach: we create a resilient function that does not merely take the top of the order book, but analyzes its depth and weights each level.

Below is an example in Python that can be integrated into your Unified Framework."

**Python: Calculate Weighted Multi-Level Order Book Imbalance.**

"This script calculates the imbalance, assigning greater weight to orders closer to the current price (spread)."

### Original illustrative Python, with English comments and docstring

The calculations, variable names and sample inputs below correspond to the source. **Only the Russian comments/docstrings have been translated**; this remains an untested research example.

```python
import numpy as np

def calculate_weighted_obi(order_book, levels=10, decay_factor=0.5):
    """
    order_book: dictionary with 'bids' and 'asks' as [[price, size], ...]
    levels: number of order-book levels to analyze
    decay_factor: weight decay coefficient (farther levels matter less)
    """
    bids = np.array(order_book['bids'][:levels])
    asks = np.array(order_book['asks'][:levels])

    # Extract volumes (size)
    bid_volumes = bids[:, 1]
    ask_volumes = asks[:, 1]

    # Generate weights: [1.0, 0.5, 0.25, 0.125, ...]
    weights = np.array([decay_factor**i for i in range(levels)])

    # Weighted volumes
    weighted_bid_vol = np.sum(bid_volumes * weights)
    weighted_ask_vol = np.sum(ask_volumes * weights)

    # OBI formula: (Bid - Ask) / (Bid + Ask)
    obi = (weighted_bid_vol - weighted_ask_vol) / (weighted_bid_vol + weighted_ask_vol)

    return round(obi, 4)

# Example data from Bybit WebSocket
sample_book = {
    'bids': [[30000.5, 1.2], [30000.0, 5.0], [29999.5, 10.0]],
    'asks': [[30001.0, 0.5], [30001.5, 2.0], [30002.0, 4.0]]
}

current_obi = calculate_weighted_obi(sample_book, levels=3)
print(f"Current Weighted OBI: {current_obi}")

# If result > 0: buy-side dominance
# If result < 0: sell-side dominance
```

**Implementation limitation, not part of the translation:** The historical snippet does not guard against empty/short order-book sides, mismatched level counts or a zero denominator. The number of weights must agree with the number of actual volume entries. These cases require separate tested implementation work before real trading.

## 2. How the source proposes using OBI (lines 278–310)

### 2.1 HistGBT — classification

"For this project, OBI becomes a key feature of the models.

Add an `obi_imbalance` column to the training dataset.

**Example:** If `obi < -0.7` on Bybit while the bot intends to buy the asset for arbitrage, **HistGBT issues a Veto**. The interpretation offered was that buy-side liquidity is thin and the price may fall."

The source presents this as **proposed classification logic**, not empirical evidence of a predictive threshold.

### 2.2 TFT — time-series prediction

"Feed OBI as a data stream.

**Example:** The model might detect that OBI sharply shifts to `+0.9` five seconds before a pump. This could provide the arbitrage system with several hundred milliseconds of advantage over competitors."

This is an illustration of an intended feature and hypothesis, not a demonstrated market behavior, measured prediction accuracy or latency guarantee.

### 2.3 DRL — execution optimization

"The DRL agent reads OBI to assess **urgency**.

**Example:** When `obi` is extreme, close to either +1 or -1, the agent may infer that liquidity is disappearing rapidly. It could choose a **market order instead of a limit order** to close the route, accepting potentially greater slippage."

**Source caveat:** The original wording described this as guaranteeing completion. Market orders can still partially fill, incur unexpectedly severe slippage or fail, so **no execution guarantee is implied by the translation**. The proposal must remain subject to risk caps, exchange constraints and transaction-level checks.

### 2.4 QA recommendation — ghost liquidity

"As Test Manager, add a **Ghost Liquidity** check:

1. Create an **OBI Delta** metric measuring the rate of imbalance change.
2. If OBI changes from `+0.8` to `-0.8` in less than 100 ms while executed volume is zero, flag a possible algorithmic spoofing/manipulation event.
3. In that case, the AI agent might temporarily increase `Slippage_Tolerance` or stop trading the pair."

The first-person original claimed this would make the system substantially better than ordinary scripts and then asked about configuring a DRL agent to select an exchange path. The example is a **heuristic proposal**. A rapid OBI change is not proof of spoofing, and increasing slippage can increase losses. Prefer controlled research/evaluation and a fail-closed response until independently validated.

## 3. Language and development-stack note (lines 500–504)

"Understood. This is a good approach for professional development: technical documentation and code are written in English (for broad developer accessibility), while discussion and explanations can take place in Russian for efficient comprehension.

As Test Manager, you will appreciate this plan because it is structured by system-responsibility layers.

Below is the final consolidated architecture document for the Unified Intelligent Arbitrage Framework."

This is an **historic discussion preference**, not an authorization to add Russian prose to new GitHub content. The current repository language policy remains English for newly authored GitHub-facing text.

## 4. Russian commentary on the consolidated architecture (lines 620–633)

The source labeled this section "Explanations in Russian." Its substantive English translation is:

1. **Intelligence & Prediction Layers:** HistGBT filters out poor-quality candidates; TFT (as originally planned) forecasts Bybit price direction. The proposed arbitrage module is a "sandbox" because a completed trade rapidly indicates whether a modeled opportunity produced positive or negative P&L, which could speed up model iteration.
2. **Whale Tracker:** Added as a "trigger." Rather than continuously scanning without context, the bot can react when a large participant makes a significant move.
3. **Slippage & MEV:** Presented as protection. The source claimed Flashbots would prevent a harmful price-move transaction from appearing on-chain, avoiding wasted gas.
4. **Weighted OBI:** Proposed as a high-value ML feature on the theory that Bybit order-book imbalance forecasts short-horizon price direction (around ten seconds).

**Next-step request in the source:** Begin implementing an asynchronous **Data Streamer** consuming Bybit Level-2 WebSocket order books to supply training observations to HistGBT.

A later discussion then acknowledged a performance/maintenance concern and stated:

"Fair point. As QA Automation Lead, you're right: if a tool cannot tolerate the load or adds unjustified maintenance complexity, it should be replaced. **DuckDB** was selected as a good option for in-process/local OLAP work, enabling fast analysis of large data volumes without maintaining a full database server.

I relied on outdated information in earlier logs and drafts. We now record that your stack is based on **DuckDB**. Below is a revised English integration plan with additional Russian explanations."

This describes the plan **at that point in the source conversation**. It does not certify the current codebase storage architecture.

## 5. DuckDB commentary and integration context (lines 711–723)

The next section was again headed "Explanations in Russian." Translated:

1. **Why DuckDB for this proposal?** Arbitrage research repeatedly asks what happened one minute earlier. DuckDB can perform analytical SQL over local/in-memory datasets quickly, reducing some overhead of a separate database service for this kind of query.
2. **Zero-copy with Apache Arrow:** DuckDB can integrate with Polars/Pandas Arrow-compatible data without unnecessary duplication in some supported paths. The research text proposed this as a way to reduce latency.
3. **Slippage Guard:** Keep a history of observed trade slippage in a compact DuckDB table and compute an average before new trades.

**Next-step question recorded by the source:** With a proposed Bybit + DuckDB + HistGBT/DRL + Flashbots stack, should it prepare `arbitrage_strategy.py` for the GitHub strategy directory?

The next two Russian paragraphs assert, as a historical speaker's observation: "I examined your latest push. From a QA Automation Lead perspective, the project structure has become more mature, especially in data handling and model architecture. Below is a detailed report on integrating the existing code with the proposed arbitrage strategy."

No commit ID, actual run or exact source verification accompanies that assertion in `arbitrage.txt`; it must **not** be treated as a code review performed in this translation.

## 6. Integration analysis — existing bot plus arbitrage module (lines 727–761)

### Report introduction

"This report describes how the latest repository changes fit together with the proposed **Weighted OBI**, **Whale Tracker**, and **Slippage Guard** modules."

### 6.1 Claimed strengths of the existing implementation

The historical report asserted that roughly **60% of the infrastructure tasks** were already covered. It then identified:

- **QuestDB integration:** Recording ticks and candles was described as implemented. The proposal wanted to record L2 order-book/OBI events for HistGBT features.
- **Model management:** A TFT-oriented model structure was said to exist and could support a macro trend predictor.
- **Bybit API layer:** Connectors were described as stable, with streaming Level-2 WebSocket support still needed.

The **60% number and component readiness are unverified claims from the original research note**, not a recalculated status.

### 6.2 Proposed convergence

"Use the current trading bot as the base platform, adding arbitrage as a new **strategy class** rather than rewriting the entire application."

**Step 1 — Transform the data pipeline.**

- **Weighted OBI:** Calculate order-book imbalance as WebSocket data arrives; feed the result into QuestDB and HistGBT in parallel.
- **Feature engineering:** Extend existing model inputs with `obi_weighted`, `gas_price` and `whale_sentiment`.

**Step 2 — Integrate DRL and HistGBT.**

The proposed filtering sequence is:

1. **TFT** says, "The trend is upward."
2. **Whale Tracker** observes a large DEX purchase.
3. **HistGBT** checks that the Bybit order book is deep enough and predicts that the arbitrage window may remain open for ten seconds.
4. **DRL** chooses a preferred exchange path and position size.

These are model roles in an **architectural hypothesis**, not reliable statements that any current model produces those predictions.

The original section headed "Technical Specification (English Documentation)" was already English. It describes **QuestDB** for L2 time-series storage, Bybit WebSocket as a reference price, private RPC/Flashbots for the DEX leg, and exchange APIs for CEX hedging. Its three conceptual levels are TFT macro, HistGBT micro/OBI veto, and DRL multi-hop path selection.

## 7. Next-commit request and commentary (lines 781–796)

### What should be added in the next commit?

"To integrate successfully, expand the exchange connector:

1. **Async WebSocket Connector:** Stream roughly 10–20 order-book levels.
2. **Circular Buffer:** Keep the most recent 100 OBI values in a fast in-memory buffer, so the models can observe changes in the imbalance and its rate."

These are proposals in the research file, not proof of implementation or permission to modify production exchange connectivity.

### Historical concluding comment

"You have already done substantial work on **QuestDB** and **TFT**, providing the foundation. The new arbitrage component would attach as a high-speed module.

**Main recommendation:** Do not rewrite everything as one file. Use a separate `strategies/arbitrage` directory for **Slippage Guard** and **Flashbots** logic. Keep the primary trading bot as the core, with arbitrage as a high-speed plugin."

The source ended by asking whether to prepare the `arbitrage_strategy.py` file layout for this architecture. That question did not itself authorize the code change.

## 8. Editorial cautions (not translations or approvals)

1. **DuckDB versus QuestDB:** Different portions of this historical file propose different storage choices. The last integration section specifically says QuestDB, despite an earlier explicit DuckDB correction. Do not silently select either one as the live implementation. Reconcile against [current architecture](../core/ARCHITECTURE.md) and actual code.
2. **Exchange and MEV claims:** The source presents Flashbots inclusion and gas savings, arbitrary execution certainty, and a near-real-time market advantage too strongly. All are contingent on network, RPC, chain, bundle, exchange, latency and slippage behavior; none is certified here.
3. **Model confidence:** Thresholds such as `obi < -0.7`, `+0.9`, and a 100 ms spoofing condition are illustrative hypotheses. Validate on separate market regimes, with fees and adverse selection accounted for.
4. **Code safety:** The example function is instructional and was not executed or integrated. Test missing/empty books, zero volumes, inconsistent depth, order sorting, sequence gaps, stale data and partial fills before production use.
5. **History and permissions:** The original large mixed-language `arbitrage.txt` is retained byte-for-byte. New GitHub-facing prose is English, while literal market fields and technical identifiers are kept exact. No exchange keys, database schemas, CI configuration or trading permissions were changed.

**Review status:** English translation of the historical Russian-language passages **COMPLETE FOR DOCUMENTARY REVIEW**, not independently signed off for semantic fidelity, current architecture or live trading.
