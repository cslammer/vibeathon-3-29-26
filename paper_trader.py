#!/usr/bin/env python3
"""
Bull/Bear/Judge Paper Trading Bot
===================================
Two modes in one file:

  --v1   Original architecture: bull vs bear debate, flat position sizing
  --v2   Epistemic Organism:
           * Epistemic Scout    – scores markets by resolvability before debating
           * Devil's Advocate   – attacks whichever side is winning mid-debate
           * Adaptive weights   – personas re-weighted from resolved trade history
           * Kelly sizing       – fractional Kelly + portfolio correlation guard
           * Self-critique      – on resolve, re-analyses what worked, stores lessons
           * Lessons feed       – top lessons injected into next run's system prompts

Usage
-----
  python3 paper_trader.py --v1 --scan        # original scan + trade
  python3 paper_trader.py --v1 --report      # original report
  python3 paper_trader.py --v1 --resolve     # score resolved trades (v1 ledger)
  python3 paper_trader.py --v1 --demo        # interactive debate (v1)

  python3 paper_trader.py --v2 --scan        # new epistemic scan + trade
  python3 paper_trader.py --v2 --report      # v2 report with persona stats
  python3 paper_trader.py --v2 --resolve     # score + self-critique (v2 ledger)
  python3 paper_trader.py --v2 --demo        # interactive debate (v2)

Setup
-----
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 paper_trader.py --v2 --scan
"""

import os, json, argparse, urllib.request, urllib.error, time, math
from datetime import datetime, timezone

# ─────────────────────────────────────────────
#  SHARED CONFIG
# ─────────────────────────────────────────────

ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "YOUR_KEY_HERE")
STARTING_BANKROLL   = 1000.0
DEBATE_ROUNDS       = 1
CANDIDATES_TO_SCAN  = 5
MAX_TRADES_PER_RUN  = 2

# v1 config
V1_MAX_TRADE_PCT    = 0.03
V1_MIN_CONFIDENCE   = 0.55
V1_MIN_EDGE_PCT     = 0.03
V1_LEDGER_FILE      = "paper_ledger_v1.json"

# v2 config
V2_KELLY_FRACTION   = 0.25      # fractional Kelly multiplier (conservative)
V2_MIN_CONFIDENCE   = 0.57
V2_MIN_EDGE_PCT     = 0.03
V2_MAX_EXPOSURE_PCT = 0.10      # hard cap per trade regardless of Kelly
V2_MAX_CATEGORY_PCT = 0.20      # max % bankroll in correlated markets
V2_MIN_SCOUT_SCORE  = 0.35      # markets scoring below this are skipped
V2_LEDGER_FILE      = "paper_ledger_v2.json"

SPORTS_KEYWORDS = [
    # Governing bodies / leagues by name
    "FIFA","UEFA","NFL","NBA","MLB","NHL","MLS","EPL","La Liga",
    "Serie A","Bundesliga","Champions League","World Cup","Super Bowl",
    "Stanley Cup","March Madness","NCAAB","NCAAF",
    "UFC","MMA","boxing","wrestling","NASCAR","Formula 1",
    "Olympics","Tour de France",
    # Betting line markers (always sports)
    "Spread:","Moneyline:","moneyline","over/under","Over/Under",
    # Sports-specific phrases
    "match winner","season wins","hat trick","home run",
    "strikeout","touchdowns","goals scored",
    # Tennis tournament matchups  (pattern: "Name vs Name" for tennis events)
    "Open: ","Masters 1000","Grand Slam","Davis Cup","Fed Cup",
    # Common team names that only appear in sports contexts
    "Trail Blazers","Lakers","Celtics","Warriors","Knicks","Nuggets",
    "Chiefs","Cowboys","Patriots","Eagles","49ers","Packers",
    "Yankees","Red Sox","Dodgers","Cubs","Mets","Astros",
    "Maple Leafs","Bruins","Penguins","Avalanche",
]

# ─────────────────────────────────────────────
#  HTTP HELPERS
# ─────────────────────────────────────────────

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

def http_get(url):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} -> {url[:80]}")
        return None
    except Exception as e:
        print(f"  Request error: {e}")
        return None

def http_post(url, payload, extra_headers=None):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **(extra_headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"  Request error: {e}")
        return None

# ─────────────────────────────────────────────
#  CLAUDE API
# ─────────────────────────────────────────────

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL  = "claude-opus-4-5"

def call_claude(system, messages, label="", max_tokens=700):
    print(f"  [AI] {label} ...")
    result = http_post(
        ANTHROPIC_URL,
        {"model": CLAUDE_MODEL, "max_tokens": max_tokens,
         "system": system, "messages": messages},
        extra_headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    if not result:
        raise RuntimeError(f"Claude API failed [{label}]")
    return result["content"][0]["text"]

def parse_json_response(raw, label=""):
    clean = raw.strip()
    if clean.startswith("```"):
        parts = clean.split("```")
        clean = parts[1] if len(parts) > 1 else clean
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        print(f"  WARNING: {label} returned invalid JSON:\n{raw[:300]}")
        raise

# ─────────────────────────────────────────────
#  POLYMARKET DATA  (shared)
# ─────────────────────────────────────────────

GAMMA_MARKETS_URL = (
    "https://gamma-api.polymarket.com/markets"
    "?active=true&closed=false&limit=100"
    "&order=volume24hr&ascending=false"
)

def is_sports(question):
    q = question.lower()
    return any(kw.lower() in q for kw in SPORTS_KEYWORDS)

def _parse_yes_price(m):
    try:
        raw = m.get("outcomePrices")
        if raw:
            prices = json.loads(raw) if isinstance(raw, str) else raw
            if prices:
                return float(prices[0])
    except Exception:
        pass
    for field in ("lastTradePrice", "bestBid", "price"):
        try:
            v = m.get(field)
            if v is not None:
                return float(v)
        except Exception:
            pass
    return None

def _parse_volume(m):
    try:
        return float(m.get("volume24hr") or m.get("volume") or 0)
    except Exception:
        return 0.0

def fetch_raw_markets(n=20):
    """Return up to n non-sports binary markets sorted by volume."""
    print("Fetching markets from Polymarket ...")
    data = http_get(GAMMA_MARKETS_URL)
    if not data:
        return []
    markets_raw = data if isinstance(data, list) else data.get("data", [])
    markets_raw = sorted(markets_raw, key=_parse_volume, reverse=True)

    out = []
    for m in markets_raw:
        if not m.get("active") or m.get("closed"):
            continue
        question = m.get("question", "").strip()
        if not question or is_sports(question):
            continue
        try:
            outcomes_raw = m.get("outcomes")
            if outcomes_raw:
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                if len(outcomes) != 2:
                    continue
        except Exception:
            pass
        yes_price = _parse_yes_price(m)
        if yes_price is None or yes_price <= 0.02 or yes_price >= 0.98:
            continue
        cid = m.get("conditionId") or m.get("condition_id", "")
        if not cid:
            continue
        yes_token_id = no_token_id = None
        try:
            clob_raw = m.get("clobTokenIds")
            if clob_raw:
                ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
                if len(ids) >= 2:
                    yes_token_id, no_token_id = ids[0], ids[1]
        except Exception:
            pass
        out.append({
            "condition_id":  cid,
            "question":      question,
            "yes_price":     yes_price,
            "volume24hr":    _parse_volume(m),
            "yes_token_id":  yes_token_id,
            "no_token_id":   no_token_id,
            "end_date":      m.get("endDate") or m.get("end_date") or "",
        })
        if len(out) >= n:
            break
    return out

# ─────────────────────────────────────────────
#  LEDGER  (shared helpers)
# ─────────────────────────────────────────────

def load_ledger(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {
        "bankroll":          STARTING_BANKROLL,
        "starting_bankroll": STARTING_BANKROLL,
        "trades":            [],
        "created":           datetime.now(timezone.utc).isoformat(),
    }

def save_ledger(ledger, path):
    with open(path, "w") as f:
        json.dump(ledger, f, indent=2)


# ==============================================================
#
#   V1  --  ORIGINAL ARCHITECTURE
#
# ==============================================================

V1_BULL_SYSTEM = """You are a sharp BULL analyst in a structured prediction market debate.
Role: argue the outcome resolves YES.
- 2-3 strongest evidence-based arguments
- Use base rates, trends, recent data
- Rebut bear points when provided
- Under 200 words. End with "Bull thesis: <one sentence>"
"""

V1_BEAR_SYSTEM = """You are a sharp BEAR analyst in a structured prediction market debate.
Role: argue the outcome resolves NO.
- 2-3 strongest evidence-based arguments
- Use base rates, tail risks, timing problems
- Rebut bull points when provided
- Under 200 words. End with "Bear thesis: <one sentence>"
"""

V1_JUDGE_SYSTEM = """You are an impartial judge evaluating a prediction market debate.
Respond with ONLY valid JSON -- no markdown, no extra text:
{
  "verdict": "YES" or "NO",
  "our_probability": <float 0-1>,
  "confidence": <float 0-1>,
  "edge_detected": <bool>,
  "reasoning": "<2-3 sentences>",
  "key_bull_point": "<one sentence>",
  "key_bear_point": "<one sentence>"
}
Be conservative -- if close or uncertain, set confidence <= 0.55 and edge_detected = false.
"""

def v1_run_debate(question, market_price=None):
    print(f"\n{'='*62}")
    print(f"  {question}")
    if market_price is not None:
        print(f"  Market price: {market_price:.1%} YES")
    print(f"{'='*62}")

    transcript = [
        f"PREDICTION MARKET QUESTION: {question}",
        f"Current market YES price: {market_price:.1%}" if market_price else "",
        "",
    ]
    bull_msgs = [{"role": "user", "content": f"Question: {question}\n\nMake your opening bull argument."}]
    bear_msgs = [{"role": "user", "content": f"Question: {question}\n\nMake your opening bear argument."}]

    for r in range(1, DEBATE_ROUNDS + 1):
        print(f"\n  -- Round {r} --")
        bull_arg = call_claude(V1_BULL_SYSTEM, bull_msgs, f"Bull R{r}")
        print(f"\n  BULL: {bull_arg[:200]}{'...' if len(bull_arg) > 200 else ''}")
        transcript.append(f"BULL (Round {r}):\n{bull_arg}\n")

        bear_msgs.append({"role": "user", "content": f"The bull argued:\n{bull_arg}\n\nNow make your bear case and rebut."})
        bear_arg = call_claude(V1_BEAR_SYSTEM, bear_msgs, f"Bear R{r}")
        print(f"\n  BEAR: {bear_arg[:200]}{'...' if len(bear_arg) > 200 else ''}")
        transcript.append(f"BEAR (Round {r}):\n{bear_arg}\n")

        bull_msgs.append({"role": "assistant", "content": bull_arg})
        bull_msgs.append({"role": "user", "content": f"The bear responded:\n{bear_arg}\n\nStrengthen your bull case."})
        bear_msgs.append({"role": "assistant", "content": bear_arg})

    print("\n  Judge deliberating ...")
    raw = call_claude(V1_JUDGE_SYSTEM, [{"role": "user", "content": "\n".join(transcript)}], "Judge")
    judgment = parse_json_response(raw, "V1 Judge")

    print(f"\n  VERDICT:    {judgment['verdict']}")
    print(f"  Our prob:   {judgment['our_probability']:.1%}")
    print(f"  Confidence: {judgment['confidence']:.1%}")
    print(f"  Edge found: {judgment['edge_detected']}")
    print(f"  Reasoning:  {judgment['reasoning']}")
    return judgment

def v1_should_trade(judgment, market_price):
    if judgment["confidence"] < V1_MIN_CONFIDENCE:
        return False, f"confidence {judgment['confidence']:.1%} < {V1_MIN_CONFIDENCE:.1%}"
    if not judgment["edge_detected"]:
        return False, "no edge detected"
    edge = abs(judgment["our_probability"] - market_price)
    if edge < V1_MIN_EDGE_PCT:
        return False, f"edge {edge:.1%} < {V1_MIN_EDGE_PCT:.1%}"
    return True, "ok"

def v1_record_trade(ledger, market, judgment, side, size):
    entry_price = market["yes_price"] if side == "YES" else round(1 - market["yes_price"], 4)
    trade = {
        "id":              len(ledger["trades"]) + 1,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "condition_id":    market["condition_id"],
        "question":        market["question"],
        "side":            side,
        "size_usdc":       round(size, 2),
        "entry_price":     entry_price,
        "our_probability": judgment["our_probability"],
        "confidence":      judgment["confidence"],
        "reasoning":       judgment["reasoning"],
        "status":          "open",
        "pnl":             None,
        "yes_token_id":    market.get("yes_token_id"),
        "no_token_id":     market.get("no_token_id"),
    }
    ledger["bankroll"] -= size
    ledger["trades"].append(trade)
    save_ledger(ledger, V1_LEDGER_FILE)
    print(f"\n  Paper trade #{trade['id']}: {side} ${size:.2f} @ {entry_price:.3f}")
    print(f"  {market['question'][:72]}")
    return trade

def v1_scan_and_trade():
    ledger = load_ledger(V1_LEDGER_FILE)
    print(f"  [v1] Bankroll: ${ledger['bankroll']:.2f}\n")
    markets = fetch_raw_markets(n=20)
    already = {t["condition_id"] for t in ledger["trades"]}
    candidates = [m for m in markets if m["condition_id"] not in already][:CANDIDATES_TO_SCAN]
    if not candidates:
        print("No new markets found.")
        return
    print(f"  Found {len(candidates)} candidates:")
    for i, c in enumerate(candidates):
        print(f"  {i+1}. [{c['yes_price']:.0%} YES] {c['question'][:65]}")

    traded = 0
    for i, market in enumerate(candidates):
        if traded >= MAX_TRADES_PER_RUN:
            break
        print(f"\n  Market {i+1}/{len(candidates)}")
        try:
            judgment = v1_run_debate(market["question"], market_price=market["yes_price"])
        except Exception as e:
            print(f"  Debate failed: {e}")
            continue
        do_trade, reason = v1_should_trade(judgment, market["yes_price"])
        if not do_trade:
            print(f"\n  No trade -- {reason}")
            time.sleep(1)
            continue
        side = judgment["verdict"]
        size = round(ledger["bankroll"] * V1_MAX_TRADE_PCT, 2)
        size = max(1.0, min(size, ledger["bankroll"] * 0.10))
        v1_record_trade(ledger, market, judgment, side, size)
        traded += 1
        time.sleep(1)
    print(f"\nDone -- {traded} paper trade(s) placed.")
    v1_print_report()

def v1_resolve():
    ledger = load_ledger(V1_LEDGER_FILE)
    open_trades = [t for t in ledger["trades"] if t["status"] == "open"]
    if not open_trades:
        print("No open trades.")
        return
    print(f"\nChecking {len(open_trades)} open trade(s) ...\n")
    for trade in open_trades:
        data = http_get(f"https://clob.polymarket.com/markets/{trade['condition_id']}")
        if not data:
            print(f"  #{trade['id']} -- could not fetch")
            continue
        if not data.get("resolved") or data.get("resolution") is None:
            print(f"  #{trade['id']} still open -- {trade['question'][:55]}")
            continue
        resolution = data["resolution"]
        won = trade["side"] == resolution
        pnl = (trade["size_usdc"] * (1 / trade["entry_price"] - 1)) if won else -trade["size_usdc"]
        trade.update(status="won" if won else "lost", resolution=resolution, pnl=round(pnl, 2))
        ledger["bankroll"] += trade["size_usdc"] + pnl
        print(f"  #{trade['id']} {'WON' if won else 'LOST'} | PnL ${pnl:+.2f} | {trade['question'][:50]}")
    save_ledger(ledger, V1_LEDGER_FILE)
    print("\nLedger saved.")

def v1_print_report():
    ledger    = load_ledger(V1_LEDGER_FILE)
    trades    = ledger["trades"]
    closed    = [t for t in trades if t["status"] in ("won", "lost")]
    open_t    = [t for t in trades if t["status"] == "open"]
    won       = [t for t in closed if t["status"] == "won"]
    total_pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
    win_rate  = len(won) / len(closed) if closed else 0
    ret_pct   = (ledger["bankroll"] / ledger["starting_bankroll"] - 1) * 100
    print(f"""
==================================================
  V1 PERFORMANCE REPORT
==================================================
  Starting bankroll : ${ledger['starting_bankroll']:>10.2f}
  Current bankroll  : ${ledger['bankroll']:>10.2f}
  Total PnL         : ${total_pnl:>+10.2f}
  Return            : {ret_pct:>+9.1f}%
  Total trades      : {len(trades)}  Open: {len(open_t)}  Won: {len(won)}  Lost: {len(closed)-len(won)}
  Win rate          : {win_rate:.1%}
""")
    if open_t:
        print("  Open positions:")
        for t in open_t:
            print(f"  #{t['id']} {t['side']} ${t['size_usdc']:.2f} @ {t['entry_price']:.3f}  {t['question'][:55]}")
    print()

def v1_demo():
    print("\n[v1] DEMO MODE -- debates only, nothing recorded\n")
    while True:
        try:
            q = input("Question (or quit): ").strip()
            if q.lower() in ("quit", "q", "exit", ""):
                break
            p = input("YES price 0-1 (Enter to skip): ").strip()
            price = float(p) if p else None
            judgment = v1_run_debate(q, market_price=price)
            print("\n" + json.dumps(judgment, indent=2) + "\n")
        except KeyboardInterrupt:
            break


# ==============================================================
#
#   V2  --  EPISTEMIC ORGANISM ARCHITECTURE
#
# ==============================================================

# ── System prompts ─────────────────────────────────────────────

V2_SCOUT_SYSTEM = """You are an Epistemic Scout for a prediction market trading system.
Score how tractable this market is for AI reasoning.

SCORE HIGH (0.6-0.9) if:
- Resolves on verifiable public facts (government actions, economic data, elections, policy decisions)
- Has clear, unambiguous resolution criteria
- Is a political, economic, geopolitical, or technology question
- There is publicly available information that could shift probability estimates

SCORE MEDIUM (0.4-0.6) if:
- Resolves on somewhat ambiguous criteria
- Is a reasonable question but resolution timing is unclear
- Mixed signals available

SCORE LOW (0.1-0.4) if:
- It is a sports spread, sports matchup, or sports player prop bet
- Resolution criteria are genuinely ambiguous or private
- No public information could reasonably shift the estimate

NOTE: Political, economic, regulatory, and geopolitical markets should generally score 0.5+.
Uncertainty about the OUTCOME does not make a market untractable -- we want uncertain markets.

Respond with ONLY valid JSON -- no markdown:
{
  "resolvability_score": <float 0-1>,
  "info_edge_potential": <float 0-1>,
  "ambiguity_risk": <float 0-1>,
  "overall_score": <float 0-1>,
  "reasoning": "<2 sentences max>",
  "recommended": <bool>
}
"""

def _build_bull_system_v2(weight, lessons):
    lesson_str = ""
    if lessons:
        lesson_str = "\n\nLessons from past resolved trades:\n" + "\n".join(f"- {l}" for l in lessons[:3])
    return (
        f"You are a BULL analyst in a prediction market debate. "
        f"Your persuasion weight this session: {weight:.2f}/1.0 "
        f"(higher = your past arguments have been more accurate).\n\n"
        "Role: argue the outcome resolves YES.\n"
        "- 2-3 strongest evidence-based arguments\n"
        "- Use base rates, trends, recent data\n"
        "- Rebut bear points when provided\n"
        f"- Under 200 words. End with \"Bull thesis: <one sentence>\"{lesson_str}\n"
    )

def _build_bear_system_v2(weight, lessons):
    lesson_str = ""
    if lessons:
        lesson_str = "\n\nLessons from past resolved trades:\n" + "\n".join(f"- {l}" for l in lessons[:3])
    return (
        f"You are a BEAR analyst in a prediction market debate. "
        f"Your persuasion weight this session: {weight:.2f}/1.0 "
        f"(higher = your past arguments have been more accurate).\n\n"
        "Role: argue the outcome resolves NO.\n"
        "- 2-3 strongest evidence-based arguments\n"
        "- Use base rates, tail risks, timing problems\n"
        "- Rebut bull points when provided\n"
        f"- Under 200 words. End with \"Bear thesis: <one sentence>\"{lesson_str}\n"
    )

def _build_devils_advocate_system(winning_side):
    return (
        f"You are the Devil's Advocate in a prediction market debate.\n"
        f"The {winning_side} side is currently winning. "
        "Your ONLY job is to undermine them.\n\n"
        f"Find the 2 weakest points in the {winning_side} argument and attack them hard.\n"
        "Introduce one overlooked risk or counterexample the other agents have ignored.\n"
        "Under 150 words. End with \"Devil's point: <one sentence>\"\n"
    )

V2_JUDGE_SYSTEM = """You are an impartial judge evaluating a three-way prediction market debate (Bull, Bear, Devil's Advocate).
Weigh each argument by the persona's stated persuasion weight.

Respond with ONLY valid JSON -- no markdown:
{
  "verdict": "YES" or "NO",
  "our_probability": <float 0-1>,
  "confidence": <float 0-1>,
  "edge_detected": <bool>,
  "winning_persona": "bull" or "bear",
  "reasoning": "<2-3 sentences>",
  "key_bull_point": "<one sentence>",
  "key_bear_point": "<one sentence>",
  "devil_impact": "<one sentence -- did the devil's advocate change anything?>"
}
Be conservative. If uncertain, confidence <= 0.57, edge_detected = false.
"""

V2_META_JUDGE_SYSTEM = """You are a Meta-Judge reviewing a primary judge's verdict on a prediction market.
Your job: catch overconfidence, anchoring bias, or reasoning errors.

Respond with ONLY valid JSON -- no markdown:
{
  "veto": <bool>,
  "adjusted_confidence": <float 0-1>,
  "adjusted_probability": <float 0-1>,
  "bias_detected": "<one sentence or 'none'>",
  "veto_reason": "<one sentence if veto=true, else null>"
}
Only veto if there is a clear logical flaw, the confidence is > 0.80 with little evidence,
or the reasoning contradicts the stated probability. Be rare with vetoes.
"""

V2_CRITIQUE_SYSTEM = """You are a trading post-mortem analyst. A prediction market trade has resolved.
Read the original debate summary and actual outcome, then extract lessons.

Respond with ONLY valid JSON -- no markdown:
{
  "winning_side_was": "bull" or "bear",
  "what_worked": "<one sentence -- what reasoning correctly predicted the outcome>",
  "what_failed": "<one sentence -- what reasoning was wrong>",
  "lesson": "<one actionable sentence to improve future debates>",
  "bull_accuracy_delta": <float -1 to 1, positive means bull was right>,
  "bear_accuracy_delta": <float -1 to 1, positive means bear was right>
}
"""

# ── Persona weight management ──────────────────────────────────

def _get_persona_weights(ledger):
    """Derive bull/bear weights from resolved trade history."""
    weights = ledger.get("persona_weights", {"bull": 0.5, "bear": 0.5})
    closed = [t for t in ledger["trades"] if t["status"] in ("won", "lost") and t.get("persona_deltas")]
    if not closed:
        return weights
    bull_delta = sum(t["persona_deltas"].get("bull", 0) for t in closed)
    bear_delta = sum(t["persona_deltas"].get("bear", 0) for t in closed)
    total = abs(bull_delta) + abs(bear_delta)
    if total == 0:
        return weights
    raw_bull = 0.5 + (bull_delta / (total * 2))
    raw_bear = 0.5 + (bear_delta / (total * 2))
    weights["bull"] = max(0.25, min(0.75, raw_bull))
    weights["bear"] = max(0.25, min(0.75, raw_bear))
    return weights

def _get_lessons(ledger):
    """Return the most recent actionable lessons stored from resolved trades."""
    lessons = []
    for t in reversed(ledger["trades"]):
        if t.get("lesson"):
            lessons.append(t["lesson"])
        if len(lessons) >= 5:
            break
    return lessons

# ── Kelly position sizer ───────────────────────────────────────

def kelly_size(bankroll, our_prob, market_price, confidence, side):
    """
    Fractional Kelly criterion.
    For a YES bet: b = (1/market_price) - 1,  p = our_prob
    Kelly fraction: f* = (b*p - (1-p)) / b
    Then scale by V2_KELLY_FRACTION * confidence and cap at V2_MAX_EXPOSURE_PCT.
    """
    try:
        if side == "YES":
            b = (1.0 / market_price) - 1.0
            p = our_prob
        else:
            b = (1.0 / (1.0 - market_price)) - 1.0
            p = 1.0 - our_prob
        f_star = (b * p - (1.0 - p)) / b
        f_star = max(0.0, f_star)
        fraction = f_star * V2_KELLY_FRACTION * confidence
        size = bankroll * fraction
        size = max(1.0, min(size, bankroll * V2_MAX_EXPOSURE_PCT))
        return round(size, 2)
    except ZeroDivisionError:
        return round(bankroll * 0.01, 2)

# ── Portfolio correlation guard ────────────────────────────────

def _category_exposure(ledger, question):
    """
    Rough category check: look for shared meaningful words with open trades.
    If similar-category exposure >= V2_MAX_CATEGORY_PCT of bankroll, block.
    """
    stop_words = {
        "will", "the", "a", "an", "by", "in", "of", "to", "be",
        "is", "on", "at", "for", "this", "that", "or", "and",
    }
    words = set(question.lower().split()) - stop_words
    total = 0.0
    for t in ledger["trades"]:
        if t["status"] != "open":
            continue
        other_words = set(t["question"].lower().split()) - stop_words
        overlap = len(words & other_words) / max(len(words), 1)
        if overlap >= 0.35:
            total += t["size_usdc"]
    max_allowed = ledger["bankroll"] * V2_MAX_CATEGORY_PCT
    return total, max_allowed

# ── Epistemic Scout ────────────────────────────────────────────

def v2_scout_market(market):
    """Score a market for tractability. Returns (score, recommended, reasoning)."""
    prompt = (
        f"Question: {market['question']}\n"
        f"Current YES price: {market['yes_price']:.1%}\n"
        f"End date: {market.get('end_date', 'unknown')}\n"
        f"24h volume: ${market['volume24hr']:,.0f}\n\n"
        "Score this market's tractability for AI-based analysis."
    )
    try:
        raw = call_claude(
            V2_SCOUT_SYSTEM,
            [{"role": "user", "content": prompt}],
            "Scout",
            max_tokens=300,
        )
        result = parse_json_response(raw, "Scout")
        score       = float(result.get("overall_score", 0.5))
        # Trust the model's recommended flag; use score threshold only as fallback
        if "recommended" in result:
            recommended = bool(result["recommended"])
        else:
            recommended = score >= V2_MIN_SCOUT_SCORE
        reasoning   = result.get("reasoning", "")
        return score, recommended, reasoning
    except Exception as e:
        print(f"  Scout error: {e}")
        return 0.5, True, ""

# ── Full V2 debate ─────────────────────────────────────────────

def v2_run_debate(question, market_price, ledger):
    print(f"\n{'='*62}")
    print(f"  {question}")
    print(f"  Market price: {market_price:.1%} YES")
    print(f"{'='*62}")

    weights = _get_persona_weights(ledger)
    lessons = _get_lessons(ledger)

    print(f"  Persona weights -- Bull: {weights['bull']:.2f}  Bear: {weights['bear']:.2f}")
    if lessons:
        print(f"  Injecting {len(lessons)} lesson(s) from past resolved trades")

    bull_sys = _build_bull_system_v2(weights["bull"], lessons)
    bear_sys = _build_bear_system_v2(weights["bear"], lessons)

    transcript = [
        f"PREDICTION MARKET: {question}",
        f"Market YES price: {market_price:.1%}",
        f"Bull weight: {weights['bull']:.2f}  Bear weight: {weights['bear']:.2f}",
        "",
    ]

    bull_msgs = [{"role": "user", "content": f"Question: {question}\n\nMake your opening bull argument."}]
    bear_msgs = [{"role": "user", "content": f"Question: {question}\n\nMake your opening bear argument."}]

    for r in range(1, DEBATE_ROUNDS + 1):
        print(f"\n  -- Round {r} --")

        bull_arg = call_claude(bull_sys, bull_msgs, f"Bull R{r}")
        print(f"\n  BULL: {bull_arg[:180]}{'...' if len(bull_arg) > 180 else ''}")
        transcript.append(f"BULL (weight={weights['bull']:.2f}, Round {r}):\n{bull_arg}\n")

        bear_msgs.append({
            "role": "user",
            "content": f"Bull argued:\n{bull_arg}\n\nMake your bear case and rebut.",
        })
        bear_arg = call_claude(bear_sys, bear_msgs, f"Bear R{r}")
        print(f"\n  BEAR: {bear_arg[:180]}{'...' if len(bear_arg) > 180 else ''}")
        transcript.append(f"BEAR (weight={weights['bear']:.2f}, Round {r}):\n{bear_arg}\n")

        # Heuristic: count confidence signals to decide who's winning
        yes_signals = sum(
            1 for w in ["strong", "clear", "likely", "will", "confident", "certain"]
            if w in bull_arg.lower()
        )
        no_signals = sum(
            1 for w in ["strong", "clear", "likely", "will", "confident", "certain"]
            if w in bear_arg.lower()
        )
        winning_side = "bull" if yes_signals >= no_signals else "bear"

        dev_sys = _build_devils_advocate_system(winning_side)
        dev_msgs = [{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Bull argued:\n{bull_arg}\n\n"
                f"Bear argued:\n{bear_arg}\n\n"
                f"Attack the {winning_side} side."
            ),
        }]
        dev_arg = call_claude(dev_sys, dev_msgs, f"Devil's Advocate R{r}")
        print(f"\n  DEVIL'S ADVOCATE (attacking {winning_side}):")
        print(f"  {dev_arg[:180]}{'...' if len(dev_arg) > 180 else ''}")
        transcript.append(f"DEVIL'S ADVOCATE (attacking {winning_side}, Round {r}):\n{dev_arg}\n")

        bull_msgs.append({"role": "assistant", "content": bull_arg})
        bull_msgs.append({
            "role": "user",
            "content": f"Bear argued:\n{bear_arg}\n\nDevil's advocate said:\n{dev_arg}\n\nStrengthen your bull case.",
        })
        bear_msgs.append({"role": "assistant", "content": bear_arg})

    # Primary judge
    print("\n  Primary Judge deliberating ...")
    raw = call_claude(
        V2_JUDGE_SYSTEM,
        [{"role": "user", "content": "\n".join(transcript)}],
        "Judge",
        max_tokens=500,
    )
    judgment = parse_json_response(raw, "Judge")

    print(f"\n  VERDICT:         {judgment['verdict']}")
    print(f"  Our probability: {judgment['our_probability']:.1%}")
    print(f"  Confidence:      {judgment['confidence']:.1%}")
    print(f"  Edge detected:   {judgment['edge_detected']}")
    print(f"  Winning persona: {judgment.get('winning_persona', '?')}")
    print(f"  Devil impact:    {judgment.get('devil_impact', '?')}")

    # Meta-judge veto check
    print("\n  Meta-Judge reviewing ...")
    meta_input = (
        f"Original judgment:\n{json.dumps(judgment, indent=2)}\n\n"
        f"Debate transcript (last portion):\n" + "\n".join(transcript[-6:])
    )
    meta_raw = call_claude(
        V2_META_JUDGE_SYSTEM,
        [{"role": "user", "content": meta_input}],
        "Meta-Judge",
        max_tokens=300,
    )
    meta = parse_json_response(meta_raw, "Meta-Judge")

    if meta.get("veto"):
        print(f"\n  !! META-JUDGE VETO: {meta.get('veto_reason', '')}")
        judgment["confidence"]    = 0.0
        judgment["edge_detected"] = False
        judgment["veto_reason"]   = meta.get("veto_reason", "")
    else:
        adj_conf = meta.get("adjusted_confidence", judgment["confidence"])
        adj_prob = meta.get("adjusted_probability", judgment["our_probability"])
        bias     = meta.get("bias_detected", "none")
        if bias.lower() != "none":
            print(f"  Meta-Judge bias flag: {bias}")
        judgment["confidence"]      = adj_conf
        judgment["our_probability"] = adj_prob
        print(f"  Meta-adjusted -- conf: {adj_conf:.1%}  prob: {adj_prob:.1%}")

    judgment["weights_used"] = weights
    return judgment

# ── Trade decision (v2) ────────────────────────────────────────

def v2_should_trade(judgment, market_price, ledger, question):
    if judgment["confidence"] < V2_MIN_CONFIDENCE:
        return False, f"confidence {judgment['confidence']:.1%} < {V2_MIN_CONFIDENCE:.1%}"
    if not judgment["edge_detected"]:
        return False, "no edge detected"
    edge = abs(judgment["our_probability"] - market_price)
    if edge < V2_MIN_EDGE_PCT:
        return False, f"edge {edge:.1%} < {V2_MIN_EDGE_PCT:.1%}"
    exposure, max_allowed = _category_exposure(ledger, question)
    if exposure >= max_allowed:
        return False, f"category exposure ${exposure:.0f} >= max ${max_allowed:.0f}"
    return True, "ok"

def v2_record_trade(ledger, market, judgment, side, size):
    entry_price = market["yes_price"] if side == "YES" else round(1 - market["yes_price"], 4)
    trade = {
        "id":              len(ledger["trades"]) + 1,
        "timestamp":       datetime.now(timezone.utc).isoformat(),
        "condition_id":    market["condition_id"],
        "question":        market["question"],
        "side":            side,
        "size_usdc":       round(size, 2),
        "entry_price":     entry_price,
        "our_probability": judgment["our_probability"],
        "confidence":      judgment["confidence"],
        "winning_persona": judgment.get("winning_persona", "?"),
        "devil_impact":    judgment.get("devil_impact", ""),
        "weights_used":    judgment.get("weights_used", {}),
        "reasoning":       judgment.get("reasoning", ""),
        "status":          "open",
        "pnl":             None,
        "lesson":          None,
        "persona_deltas":  None,
        "yes_token_id":    market.get("yes_token_id"),
        "no_token_id":     market.get("no_token_id"),
    }
    ledger["bankroll"] -= size
    ledger["trades"].append(trade)
    save_ledger(ledger, V2_LEDGER_FILE)
    print(f"\n  Paper trade #{trade['id']}: {side} ${size:.2f} @ {entry_price:.3f}  (Kelly-sized)")
    print(f"  {market['question'][:72]}")
    return trade

# ── Self-critique on resolve ───────────────────────────────────

def v2_self_critique(trade, resolution):
    """Ask Claude to analyse what worked and generate a lesson."""
    prompt = (
        f"Question: {trade['question']}\n"
        f"We bet {trade['side']} at {trade['entry_price']:.3f}\n"
        f"Our stated probability: {trade['our_probability']:.1%}\n"
        f"Winning persona in debate: {trade.get('winning_persona', 'unknown')}\n"
        f"Reasoning: {trade.get('reasoning', '')}\n"
        f"Devil's advocate impact: {trade.get('devil_impact', '')}\n\n"
        f"ACTUAL RESOLUTION: {resolution}\n\n"
        "Analyse what worked and what failed."
    )
    try:
        raw = call_claude(
            V2_CRITIQUE_SYSTEM,
            [{"role": "user", "content": prompt}],
            "Self-Critique",
            max_tokens=400,
        )
        critique = parse_json_response(raw, "Self-Critique")
        print(f"  Lesson:      {critique.get('lesson', '')}")
        print(f"  What worked: {critique.get('what_worked', '')}")
        print(f"  What failed: {critique.get('what_failed', '')}")
        return critique
    except Exception as e:
        print(f"  Self-critique error: {e}")
        return None

# ── Scan and trade (v2) ────────────────────────────────────────

def v2_scan_and_trade():
    ledger = load_ledger(V2_LEDGER_FILE)
    if "persona_weights" not in ledger:
        ledger["persona_weights"] = {"bull": 0.5, "bear": 0.5}

    print(f"  [v2] Bankroll: ${ledger['bankroll']:.2f}")
    weights = _get_persona_weights(ledger)
    print(f"  Persona weights -- Bull: {weights['bull']:.2f}  Bear: {weights['bear']:.2f}\n")

    all_markets = fetch_raw_markets(n=30)
    already = {t["condition_id"] for t in ledger["trades"]}
    fresh = [m for m in all_markets if m["condition_id"] not in already]

    scout_pool = fresh[:CANDIDATES_TO_SCAN + 8]
    print(f"\n  Running Epistemic Scout on top {len(scout_pool)} markets ...")
    scored = []
    for m in scout_pool:
        score, recommended, reasoning = v2_scout_market(m)
        m["scout_score"]       = score
        m["scout_recommended"] = recommended
        m["scout_reasoning"]   = reasoning
        tag = "OK" if recommended else "SKIP"
        print(f"  [{tag}] [score={score:.2f}] [{m['yes_price']:.0%} YES] {m['question'][:55]}")
        if recommended:
            scored.append(m)
        time.sleep(0.3)

    candidates = sorted(scored, key=lambda x: x["scout_score"], reverse=True)[:CANDIDATES_TO_SCAN]
    if not candidates:
        print("\n  No markets passed the scout filter.")
        return

    print(f"\n  {len(candidates)} market(s) cleared for full debate:")
    for i, c in enumerate(candidates):
        print(f"  {i+1}. [scout={c['scout_score']:.2f}] [{c['yes_price']:.0%} YES] {c['question'][:60]}")

    traded = 0
    for i, market in enumerate(candidates):
        if traded >= MAX_TRADES_PER_RUN:
            break
        print(f"\n  -- Market {i+1}/{len(candidates)} --")
        try:
            judgment = v2_run_debate(market["question"], market["yes_price"], ledger)
        except Exception as e:
            print(f"  Debate failed: {e}")
            continue

        do_trade, reason = v2_should_trade(judgment, market["yes_price"], ledger, market["question"])
        if not do_trade:
            print(f"\n  No trade -- {reason}")
            time.sleep(1)
            continue

        side = judgment["verdict"]
        size = kelly_size(
            ledger["bankroll"],
            judgment["our_probability"],
            market["yes_price"],
            judgment["confidence"],
            side,
        )
        v2_record_trade(ledger, market, judgment, side, size)
        traded += 1
        time.sleep(1)

    print(f"\nDone -- {traded} paper trade(s) placed.")
    v2_print_report()

# ── Resolve (v2) ──────────────────────────────────────────────

def v2_resolve():
    ledger = load_ledger(V2_LEDGER_FILE)
    open_trades = [t for t in ledger["trades"] if t["status"] == "open"]
    if not open_trades:
        print("No open trades.")
        return
    print(f"\nChecking {len(open_trades)} open trade(s) ...\n")
    for trade in open_trades:
        data = http_get(f"https://clob.polymarket.com/markets/{trade['condition_id']}")
        resolution = None

        # Primary: official resolved flag from API
        if data and data.get("resolved") and data.get("resolution") is not None:
            resolution = data["resolution"]

        # No fallback inference — only close when Polymarket officially resolves.
        # Live price changes are shown in --report MTM, not here.

        if resolution is None:
            print(f"  #{trade['id']} still open -- {trade['question'][:55]}")
            continue

        won = trade["side"] == resolution
        pnl = (trade["size_usdc"] * (1 / trade["entry_price"] - 1)) if won else -trade["size_usdc"]
        trade.update(status="won" if won else "lost", resolution=resolution, pnl=round(pnl, 2))
        ledger["bankroll"] += trade["size_usdc"] + pnl
        print(f"\n  #{trade['id']} {'WON' if won else 'LOST'} | PnL ${pnl:+.2f}")
        print(f"  {trade['question'][:60]}")

        print("  Running self-critique ...")
        critique = v2_self_critique(trade, resolution)
        if critique:
            trade["lesson"]         = critique.get("lesson")
            trade["persona_deltas"] = {
                "bull": critique.get("bull_accuracy_delta", 0),
                "bear": critique.get("bear_accuracy_delta", 0),
            }

    ledger["persona_weights"] = _get_persona_weights(ledger)
    save_ledger(ledger, V2_LEDGER_FILE)
    pw = ledger["persona_weights"]
    print(f"\n  Updated persona weights -- Bull: {pw['bull']:.2f}  Bear: {pw['bear']:.2f}")
    print("Ledger saved.")

# ── Report (v2) ───────────────────────────────────────────────

def _fetch_live_price(trade):
    """
    Fetch the LIVE mid-market price for this position from Gamma API.
    Gamma /markets?conditionId=... returns outcomePrices reflecting current
    order book mid — not the resolved value from the CLOB endpoint.

    Returns (token_price, yes_price) or (None, None) on failure.
      YES bet -> token_price = yes_price
      NO  bet -> token_price = 1 - yes_price
    """
    cid = trade["condition_id"]
    # Gamma API gives live outcomePrices (mid of order book), not resolved prices
    url = f"https://gamma-api.polymarket.com/markets?conditionId={cid}"
    data = http_get(url)
    if not data:
        return None, None
    try:
        # Returns a list; grab first match
        markets = data if isinstance(data, list) else data.get("markets", [])
        if not markets:
            return None, None
        m = markets[0]
        raw = m.get("outcomePrices")
        if not raw:
            return None, None
        prices = json.loads(raw) if isinstance(raw, str) else raw
        yes_price = float(prices[0])
        token_price = yes_price if trade["side"] == "YES" else (1.0 - yes_price)
        return round(token_price, 4), round(yes_price, 4)
    except Exception:
        return None, None


def v2_print_report():
    ledger  = load_ledger(V2_LEDGER_FILE)
    trades  = ledger["trades"]
    closed  = [t for t in trades if t["status"] in ("won", "lost")]
    open_t  = [t for t in trades if t["status"] == "open"]
    won     = [t for t in closed if t["status"] == "won"]
    weights = ledger.get("persona_weights", {"bull": 0.5, "bear": 0.5})
    lessons = _get_lessons(ledger)

    total_closed_pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
    win_rate         = len(won) / len(closed) if closed else 0

    # ── Live mark-to-market for open positions ──────────────────
    print("  Fetching live prices for open positions ...")
    mtm_rows = []
    total_cost      = 0.0
    total_mtm_value = 0.0
    total_max_value = 0.0

    for t in open_t:
        token_price, yes_price = _fetch_live_price(t)
        cost      = t["size_usdc"]
        # Shares owned = cost / entry_price  (each share pays $1 if correct)
        shares    = cost / t["entry_price"]
        if token_price is not None:
            mtm_value = shares * token_price   # what those shares are worth NOW
            unrealised_pnl = mtm_value - cost
            price_str = f"{token_price:.3f}"
        else:
            mtm_value      = cost              # fallback: use cost basis
            unrealised_pnl = 0.0
            price_str      = "n/a"
        max_value = shares * 1.0               # if token pays out at $1
        total_cost      += cost
        total_mtm_value += mtm_value
        total_max_value += max_value
        mtm_rows.append({
            "trade":          t,
            "shares":         shares,
            "token_price":    token_price,
            "price_str":      price_str,
            "mtm_value":      mtm_value,
            "unrealised_pnl": unrealised_pnl,
            "max_value":      max_value,
            "yes_price":      yes_price,
        })

    total_unrealised   = total_mtm_value - total_cost
    portfolio_mtm      = ledger["bankroll"] + total_mtm_value
    portfolio_max      = ledger["bankroll"] + total_max_value
    total_pnl_combined = total_closed_pnl + total_unrealised
    ret_pct            = (portfolio_mtm / ledger["starting_bankroll"] - 1) * 100

    print(f"""
==================================================
  V2 EPISTEMIC ORGANISM -- PERFORMANCE REPORT
==================================================
  Starting bankroll : ${ledger['starting_bankroll']:>10.2f}
  Net worth (live)  : ${portfolio_mtm:>10.2f}   <- cash + live token value
  Return            : {ret_pct:>+9.1f}%
  ──────────────────────────────────────────
  Cash (free)       : ${ledger['bankroll']:>10.2f}
  Tokens MTM        : ${total_mtm_value:>10.2f}   (cost: ${total_cost:.2f})
  Unrealised PnL    : ${total_unrealised:>+10.2f}
  Closed PnL        : ${total_closed_pnl:>+10.2f}
  Total PnL         : ${total_pnl_combined:>+10.2f}
  ──────────────────────────────────────────
  Trades: {len(trades)} total  {len(open_t)} open  {len(won)} won  {len(closed)-len(won)} lost  win rate {win_rate:.1%}

  Persona weights  Bull: {weights['bull']:.3f}  Bear: {weights['bear']:.3f}
""")

    if lessons:
        print("  Stored lessons from resolved trades:")
        for i, l in enumerate(lessons, 1):
            print(f"    {i}. {l}")
        print()

    if closed:
        print(f"  {'#':>3}  {'Side':4}  {'Size':>7}  {'Entry':>6}  {'PnL':>8}  Result  Question")
        print(f"  {'---'}  {'----'}  {'-------'}  {'------'}  {'--------'}  {'------'}  {'------'}")
        for t in closed:
            q = t["question"][:35] + ("..." if len(t["question"]) > 35 else "")
            r = "WON " if t["status"] == "won" else "LOST"
            print(f"  {t['id']:>3}  {t['side']:4}  ${t['size_usdc']:>6.2f}"
                  f"  {t['entry_price']:>6.3f}  ${t['pnl']:>+7.2f}  {r}    {q}")

    if mtm_rows:
        print(f"  Open positions (live prices):")
        print(f"  {'#':>3}  {'Side':4}  {'Cost':>7}  {'Bought@':>7}  {'Now@':>6}  {'Value':>8}  {'PnL':>9}  Question")
        print(f"  {'---'}  {'----'}  {'-------'}  {'-------'}  {'------'}  {'--------'}  {'---------'}  --------")
        for row in mtm_rows:
            t = row["trade"]
            q = t["question"][:45] + ("..." if len(t["question"]) > 45 else "")
            direction = "+" if row["unrealised_pnl"] >= 0 else ""
            print(f"  {t['id']:>3}  {t['side']:4}  ${t['size_usdc']:>6.2f}"
                  f"  {t['entry_price']:>7.3f}  {row['price_str']:>6}"
                  f"  ${row['mtm_value']:>7.2f}  {direction}${row['unrealised_pnl']:>7.2f}  {q}")

    print()

# ── Demo (v2) ─────────────────────────────────────────────────

def v2_demo():
    print("\n[v2] DEMO MODE -- epistemic organism debate, nothing recorded\n")
    dummy_ledger = load_ledger(V2_LEDGER_FILE)
    while True:
        try:
            q = input("Question (or quit): ").strip()
            if q.lower() in ("quit", "q", "exit", ""):
                break
            p = input("YES price 0-1: ").strip()
            try:
                price = float(p)
            except ValueError:
                price = 0.5
            judgment = v2_run_debate(q, price, dummy_ledger)
            print("\n" + json.dumps(judgment, indent=2) + "\n")
        except KeyboardInterrupt:
            break


# ==============================================================
#  MAIN
# ==============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Bull/Bear/Judge Paper Trader  --  v1 (original) and v2 (epistemic organism)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands
--------
  python3 paper_trader.py --v1 --scan       original scan + trade
  python3 paper_trader.py --v1 --report     original performance report
  python3 paper_trader.py --v1 --resolve    score resolved v1 trades
  python3 paper_trader.py --v1 --demo       v1 interactive debate

  python3 paper_trader.py --v2 --scan       epistemic scan + trade (NEW)
  python3 paper_trader.py --v2 --report     v2 report with persona weights + lessons
  python3 paper_trader.py --v2 --resolve    resolve + self-critique (updates weights)
  python3 paper_trader.py --v2 --demo       v2 interactive debate

Daily v2 routine
----------------
  python3 paper_trader.py --v2 --resolve && \\
  python3 paper_trader.py --v2 --scan    && \\
  python3 paper_trader.py --v2 --report
        """,
    )
    parser.add_argument("--v1",     action="store_true", help="Use original v1 architecture")
    parser.add_argument("--v2",     action="store_true", help="Use v2 epistemic organism architecture")
    parser.add_argument("--scan",   action="store_true", help="Scan markets and paper trade")
    parser.add_argument("--report", action="store_true", help="Print performance report")
    parser.add_argument("--resolve",action="store_true", help="Score resolved markets")
    parser.add_argument("--demo",   action="store_true", help="Interactive debate mode")
    args = parser.parse_args()

    if not args.v1 and not args.v2:
        parser.print_help()
        print("\n  Quickstart: python3 paper_trader.py --v2 --scan\n")
        return

    if not any([args.scan, args.report, args.resolve, args.demo]):
        parser.print_help()
        return

    version = "v2" if args.v2 else "v1"
    print(f"\n  == Paper Trader {version.upper()} ==\n")

    if version == "v1":
        if args.demo:    v1_demo()
        if args.resolve: v1_resolve()
        if args.report:  v1_print_report()
        if args.scan:    v1_scan_and_trade()
    else:
        if args.demo:    v2_demo()
        if args.resolve: v2_resolve()
        if args.report:  v2_print_report()
        if args.scan:    v2_scan_and_trade()


if __name__ == "__main__":
    main()
