#!/usr/bin/env python3
"""
Epistemic Organism Dashboard
=============================
Beautiful local web UI for the paper trader.
Cinematic live debate animation when scanning.

Usage:
    python3 dashboard.py
    python3 dashboard.py --port 8080

Zero extra dependencies — pure Python standard library.
Must be in the same folder as paper_trader.py and the ledger JSON.
"""

import os, sys, json, threading, time, queue, re
import urllib.request, urllib.error, urllib.parse
import http.server, socketserver
from datetime import datetime, timezone

PORT        = 5555
LEDGER_FILE = "paper_ledger_v2.json"
STARTING_BR = 1000.0

_scan_queue   = queue.Queue()
_scan_running = threading.Event()

# ──────────────────────────────────────────────────────────────────
#  Data helpers
# ──────────────────────────────────────────────────────────────────

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

def http_get(url):
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def load_ledger():
    if os.path.exists(LEDGER_FILE):
        with open(LEDGER_FILE) as f:
            return json.load(f)
    return {"bankroll": STARTING_BR, "starting_bankroll": STARTING_BR,
            "trades": [], "persona_weights": {"bull": 0.5, "bear": 0.5}}

def fetch_live_price(trade):
    cid  = trade["condition_id"]
    data = http_get(f"https://gamma-api.polymarket.com/markets?conditionId={cid}")
    if not data:
        return None, None
    try:
        markets = data if isinstance(data, list) else data.get("markets", [])
        if not markets:
            return None, None
        raw = markets[0].get("outcomePrices")
        if not raw:
            return None, None
        prices    = json.loads(raw) if isinstance(raw, str) else raw
        yes_price = float(prices[0])
        tok       = yes_price if trade["side"] == "YES" else (1.0 - yes_price)
        return round(tok, 4), round(yes_price, 4)
    except Exception:
        return None, None

def build_portfolio():
    ledger     = load_ledger()
    trades     = ledger["trades"]
    closed     = [t for t in trades if t["status"] in ("won","lost")]
    open_t     = [t for t in trades if t["status"] == "open"]
    won        = [t for t in closed if t["status"] == "won"]
    closed_pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
    win_rate   = len(won) / len(closed) if closed else 0

    positions, total_mtm, total_cost = [], 0.0, 0.0
    for t in open_t:
        tp, yp   = fetch_live_price(t)
        cost     = t["size_usdc"]
        shares   = cost / t["entry_price"]
        mtm_val  = shares * tp if tp is not None else cost
        unreal   = mtm_val - cost
        total_mtm  += mtm_val
        total_cost += cost
        positions.append({**t,
            "shares": round(shares,4), "token_price": tp,
            "price_str": f"{tp:.4f}" if tp else "n/a",
            "mtm_val": round(mtm_val,2), "unreal": round(unreal,2),
            "yes_price": yp})

    net_worth    = ledger["bankroll"] + total_mtm
    total_unreal = total_mtm - total_cost
    ret_pct      = (net_worth / ledger["starting_bankroll"] - 1) * 100

    lessons = []
    for t in reversed(trades):
        if t.get("lesson"):
            lessons.append(t["lesson"])
        if len(lessons) >= 5:
            break

    return {
        "bankroll": round(ledger["bankroll"],2),
        "starting": ledger["starting_bankroll"],
        "net_worth": round(net_worth,2),
        "total_mtm": round(total_mtm,2),
        "total_cost": round(total_cost,2),
        "total_unreal": round(total_unreal,2),
        "closed_pnl": round(closed_pnl,2),
        "total_pnl": round(closed_pnl + total_unreal,2),
        "ret_pct": round(ret_pct,2),
        "win_rate": round(win_rate*100,1),
        "open_count": len(open_t), "won_count": len(won),
        "lost_count": len(closed)-len(won), "total_trades": len(trades),
        "weights": ledger.get("persona_weights",{"bull":0.5,"bear":0.5}),
        "positions": positions, "closed": list(reversed(closed)),
        "lessons": lessons,
        "updated": datetime.now(timezone.utc).isoformat(),
    }

# ──────────────────────────────────────────────────────────────────
#  Scan runner — parses raw output into structured events
# ──────────────────────────────────────────────────────────────────

def classify_line(line: str) -> dict:
    """
    Turn a raw paper_trader output line into a structured event
    that the frontend can render as an animated agent card.
    """
    s = line.strip()

    # ── Agent thinking (AI call starting) ──────────────────────────
    if "[AI]" in s:
        label = s.split("[AI]")[-1].strip().rstrip(".")
        agent = "system"
        if "Bull"   in label: agent = "bull"
        elif "Bear"  in label: agent = "bear"
        elif "Devil" in label: agent = "devil"
        elif "Scout" in label: agent = "scout"
        elif "Judge" in label and "Meta" not in label: agent = "judge"
        elif "Meta"  in label: agent = "meta"
        elif "Critique" in label: agent = "critique"
        return {"type":"thinking","agent":agent,"label":label}

    # ── Bull speaking ──────────────────────────────────────────────
    if s.startswith("BULL:"):
        txt = s[5:].strip()
        return {"type":"speech","agent":"bull","text":txt}

    # ── Bear speaking ──────────────────────────────────────────────
    if s.startswith("BEAR:"):
        txt = s[5:].strip()
        return {"type":"speech","agent":"bear","text":txt}

    # ── Devil's Advocate ───────────────────────────────────────────
    if "DEVIL'S ADVOCATE" in s:
        txt = re.sub(r"DEVIL'S ADVOCATE.*?:\s*", "", s).strip()
        return {"type":"speech","agent":"devil","text":txt}

    # ── Verdict ────────────────────────────────────────────────────
    if s.startswith("VERDICT:"):
        verdict = s.split(":")[-1].strip()
        return {"type":"verdict","verdict":verdict}

    # ── Probability / confidence ───────────────────────────────────
    if s.startswith("Our probability:") or s.startswith("Our prob:"):
        val = s.split(":")[-1].strip()
        return {"type":"stat","key":"Our probability","value":val}
    if s.startswith("Confidence:"):
        val = s.split(":")[-1].strip()
        return {"type":"stat","key":"Confidence","value":val}
    if s.startswith("Edge detected:"):
        val = s.split(":")[-1].strip()
        return {"type":"stat","key":"Edge","value":val}
    if s.startswith("Winning persona:"):
        val = s.split(":")[-1].strip()
        return {"type":"stat","key":"Winning persona","value":val}

    # ── Meta-judge ─────────────────────────────────────────────────
    if "META-JUDGE VETO" in s:
        reason = s.split("VETO:")[-1].strip() if "VETO:" in s else "Vetoed"
        return {"type":"veto","reason":reason}
    if s.startswith("Meta-Judge bias flag:"):
        bias = s.split(":",1)[-1].strip()
        return {"type":"bias","text":bias}
    if s.startswith("Meta-adjusted"):
        return {"type":"meta_adjust","text":s}

    # ── Scout scoring ──────────────────────────────────────────────
    m = re.match(r"\[(OK|SKIP)\]\s*\[score=([\d.]+)\]\s*\[.*?\]\s*(.+)", s)
    if m:
        status, score, question = m.group(1), m.group(2), m.group(3)
        return {"type":"scout","status":status,"score":score,
                "question":question[:80]}

    # ── Paper trade placed ─────────────────────────────────────────
    if "Paper trade #" in s:
        return {"type":"trade","text":s}

    # ── No trade ───────────────────────────────────────────────────
    if "No trade" in s:
        reason = s.split("--")[-1].strip() if "--" in s else s
        return {"type":"notrade","reason":reason}

    # ── Round header ───────────────────────────────────────────────
    if "-- Round" in s:
        rnd = re.search(r"Round (\d+)", s)
        return {"type":"round","round": rnd.group(1) if rnd else "?"}

    # ── Market header (the ==== question block) ────────────────────
    if s.startswith("==="):
        return {"type":"separator"}
    if len(s) > 20 and not s.startswith("[") and not s.startswith("--") \
            and not s.startswith("Persona") and not s.startswith("Fetching") \
            and not s.startswith("Running") and not s.startswith("Done") \
            and not s.startswith("Market") and not s.startswith("Found") \
            and ":" not in s[:15]:
        return {"type":"market","question":s}

    # ── Done ───────────────────────────────────────────────────────
    if s.startswith("Done --"):
        return {"type":"done","msg":s}

    # ── Generic info ───────────────────────────────────────────────
    return {"type":"info","msg":s}


def run_scan_background():
    import subprocess
    _scan_running.set()
    _scan_queue.put({"type":"start","msg":"Initializing epistemic scan..."})
    try:
        proc = subprocess.Popen([sys.executable, "-u", "paper_trader.py", "--v2", "--scan"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if line.strip():
                evt = classify_line(line)
                evt["raw"] = line
                _scan_queue.put(evt)
        proc.wait()
        _scan_queue.put({"type":"complete","msg":"Scan complete."})
    except Exception as e:
        _scan_queue.put({"type":"error","msg":str(e)})
    finally:
        _scan_running.clear()

# ──────────────────────────────────────────────────────────────────
#  HTML — full cinematic debate UI
# ──────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Epistemic Organism · Paper Trader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,300;0,400;0,500;1,300&family=Syne:wght@400;500;600;700;800&family=Fraunces:ital,opsz,wght@0,9..144,300;1,9..144,300&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07090d;--bg2:#0c1018;--bg3:#101620;--bg4:#141d28;
  --border:#192230;--border2:#1e2d40;--border3:#243550;
  --text:#b8cfe0;--dim:#3a5268;--bright:#e8f4ff;
  --cyan:#00d4ff;--cyan2:#0099bb;
  --green:#00e5a0;--green2:#009966;
  --red:#ff3355;--red2:#aa1133;
  --amber:#ffc044;--amber2:#cc8800;
  --purple:#b08aff;--purple2:#7755cc;
  --pink:#ff6699;
  --bull-color:#00e5a0;--bear-color:#ff3355;
  --devil-color:#b08aff;--judge-color:#ffc044;
  --scout-color:#00d4ff;--meta-color:#ff6699;
  --fh:'Syne',sans-serif;--fm:'DM Mono',monospace;--fs:'Fraunces',serif;
}

html{background:var(--bg);color:var(--text);font-family:var(--fm);font-size:14px;scroll-behavior:smooth}
body{min-height:100vh}

/* grain */
body::after{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E");
  background-size:200px;opacity:.5}

/* ambient glow */
.glow{position:fixed;top:-30vh;left:50%;transform:translateX(-50%);
  width:70vw;height:50vh;border-radius:50%;pointer-events:none;z-index:0;
  background:radial-gradient(ellipse,rgba(0,212,255,.05) 0%,transparent 70%)}
.glow2{position:fixed;bottom:-20vh;left:10%;
  width:40vw;height:40vh;border-radius:50%;pointer-events:none;z-index:0;
  background:radial-gradient(ellipse,rgba(176,138,255,.04) 0%,transparent 70%)}

main{position:relative;z-index:1;max-width:1440px;margin:0 auto;padding:24px 24px 80px}

/* ── header ── */
header{display:flex;align-items:center;justify-content:space-between;
  padding-bottom:24px;border-bottom:1px solid var(--border);margin-bottom:28px}
.logo-title{font-family:var(--fh);font-size:20px;font-weight:800;
  letter-spacing:-.5px;color:var(--bright)}
.logo-title em{font-style:normal;color:var(--cyan)}
.logo-sub{font-size:10px;color:var(--dim);letter-spacing:.1em;text-transform:uppercase;margin-top:3px}
.hdr-right{display:flex;align-items:center;gap:12px}
.ts{font-size:11px;color:var(--dim)}
.btn{font-family:var(--fm);font-size:12px;font-weight:500;
  padding:8px 16px;border-radius:6px;cursor:pointer;border:1px solid;transition:all .15s}
.btn-ghost{background:transparent;color:var(--dim);border-color:var(--border2)}
.btn-ghost:hover{color:var(--text);border-color:var(--border3);background:var(--bg3)}
.btn-primary{background:var(--cyan);color:#000;border-color:var(--cyan);font-weight:600}
.btn-primary:hover{background:var(--cyan2);border-color:var(--cyan2)}
.btn-primary:disabled{opacity:.35;cursor:not-allowed}

/* ── KPI strip ── */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.kpi{background:var(--bg2);border:1px solid var(--border);border-radius:10px;
  padding:18px 18px 14px;position:relative;overflow:hidden;transition:border-color .2s}
.kpi:hover{border-color:var(--border3)}
.kpi::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:var(--kc,var(--border))}
.kpi.c-cyan{--kc:var(--cyan)}.kpi.c-green{--kc:var(--green)}.kpi.c-red{--kc:var(--red)}
.kpi.c-amber{--kc:var(--amber)}.kpi.c-purple{--kc:var(--purple)}
.kpi-lbl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.09em;margin-bottom:8px}
.kpi-val{font-family:var(--fh);font-size:26px;font-weight:700;letter-spacing:-1px;color:var(--bright)}
.kpi-val.pos{color:var(--green)}.kpi-val.neg{color:var(--red)}
.kpi-sub{font-size:11px;color:var(--dim);margin-top:5px}

/* ── layout grid ── */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:18px}
@media(max-width:960px){.grid2{grid-template-columns:1fr}}

/* ── panel ── */
.panel{background:var(--bg2);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.panel-head{display:flex;align-items:center;justify-content:space-between;
  padding:14px 20px;border-bottom:1px solid var(--border)}
.panel-title{font-family:var(--fh);font-size:12px;font-weight:600;
  text-transform:uppercase;letter-spacing:.09em;color:var(--text)}
.badge{font-size:10px;padding:2px 8px;border-radius:20px;
  background:var(--bg3);border:1px solid var(--border);color:var(--dim)}
.panel-body{padding:18px}
.empty{padding:24px;text-align:center;color:var(--dim);font-size:12px}

/* ── position cards ── */
.pos{background:var(--bg3);border:1px solid var(--border);border-radius:8px;
  padding:14px;margin-bottom:10px;transition:border-color .2s}
.pos:last-child{margin-bottom:0}
.pos:hover{border-color:var(--border3)}
.pos-top{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:12px}
.pos-q{font-size:12px;color:var(--bright);line-height:1.55;flex:1}
.pos-tag{font-size:10px;font-weight:600;padding:3px 9px;border-radius:20px;flex-shrink:0;letter-spacing:.05em}
.yes-tag{background:rgba(0,229,160,.1);color:var(--green);border:1px solid rgba(0,229,160,.2)}
.no-tag{background:rgba(255,51,85,.1);color:var(--red);border:1px solid rgba(255,51,85,.2)}
.pos-nums{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.pn-l{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-bottom:3px}
.pn-v{font-size:13px;font-weight:500;color:var(--text)}
.pn-v.pos{color:var(--green)}.pn-v.neg{color:var(--red)}
.pos-bar-wrap{margin-top:10px}
.pos-bar{height:3px;background:var(--border);border-radius:2px;overflow:hidden}
.pos-bar-fill{height:100%;border-radius:2px;background:var(--cyan);transition:width .5s}

/* ── weight bars ── */
.wbar-wrap{margin-bottom:14px}
.wbar-top{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-bottom:5px}
.wbar{height:5px;background:var(--border);border-radius:3px;overflow:hidden}
.wbar-fill-bull{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--green2),var(--green));transition:width .7s}
.wbar-fill-bear{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--red2),var(--red));transition:width .7s}

/* ── lessons ── */
.lesson{padding:10px 12px;margin-bottom:8px;border-radius:6px;
  background:var(--bg3);border-left:3px solid var(--purple);
  font-size:11px;line-height:1.6;color:var(--text)}
.lesson:last-child{margin-bottom:0}
.lesson-n{font-size:9px;color:var(--purple);font-weight:600;
  text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px}

/* ── trade table ── */
.ttable{width:100%;border-collapse:collapse;font-size:12px}
.ttable th{text-align:left;color:var(--dim);font-size:10px;font-weight:500;
  text-transform:uppercase;letter-spacing:.07em;padding:0 12px 10px;
  border-bottom:1px solid var(--border)}
.ttable td{padding:11px 12px;border-bottom:1px solid var(--border);vertical-align:top}
.ttable tr:last-child td{border-bottom:none}
.ttable tr:hover td{background:var(--bg3)}
.bwon{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600;
  background:rgba(0,229,160,.1);color:var(--green);border:1px solid rgba(0,229,160,.2)}
.blost{display:inline-block;padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600;
  background:rgba(255,51,85,.1);color:var(--red);border:1px solid rgba(255,51,85,.2)}

/* shimmer */
@keyframes shimmer{0%{background-position:-400px 0}100%{background-position:400px 0}}
.shimmer{background:linear-gradient(90deg,var(--bg2) 25%,var(--bg3) 50%,var(--bg2) 75%);
  background-size:400px 100%;animation:shimmer 1.4s infinite;border-radius:4px;
  height:14px;margin-bottom:8px}

/* ── DEBATE OVERLAY ─────────────────────────────────────── */
#debate-overlay{
  position:fixed;inset:0;z-index:200;
  background:rgba(7,9,13,.96);backdrop-filter:blur(12px);
  display:none;flex-direction:column;
}
#debate-overlay.active{display:flex}

.deb-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:18px 28px;border-bottom:1px solid var(--border);flex-shrink:0;
}
.deb-title{font-family:var(--fh);font-size:16px;font-weight:800;color:var(--bright);letter-spacing:-.3px}
.deb-status{display:flex;align-items:center;gap:10px;font-size:12px;color:var(--dim)}

/* agent roster bar */
.agents-bar{
  display:flex;align-items:center;gap:8px;padding:14px 28px;
  border-bottom:1px solid var(--border);flex-shrink:0;overflow-x:auto;
}
.agent-pill{
  display:flex;align-items:center;gap:7px;padding:7px 14px;border-radius:100px;
  border:1px solid var(--ac-border);background:var(--ac-bg);
  font-size:11px;font-weight:500;color:var(--ac-text);white-space:nowrap;
  transition:all .3s;opacity:.4;
}
.agent-pill.active{opacity:1;box-shadow:0 0 16px var(--ac-glow)}
.agent-pill .dot{width:7px;height:7px;border-radius:50%;background:var(--ac-text)}
.agent-pill.thinking .dot{animation:throb .8s infinite}
@keyframes throb{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.7)}}

/* colors per agent */
.ap-bull   {--ac-bg:rgba(0,229,160,.08);--ac-border:rgba(0,229,160,.2);--ac-text:var(--green);--ac-glow:rgba(0,229,160,.3)}
.ap-bear   {--ac-bg:rgba(255,51,85,.08);--ac-border:rgba(255,51,85,.2);--ac-text:var(--red);--ac-glow:rgba(255,51,85,.3)}
.ap-devil  {--ac-bg:rgba(176,138,255,.08);--ac-border:rgba(176,138,255,.2);--ac-text:var(--purple);--ac-glow:rgba(176,138,255,.3)}
.ap-judge  {--ac-bg:rgba(255,192,68,.08);--ac-border:rgba(255,192,68,.2);--ac-text:var(--amber);--ac-glow:rgba(255,192,68,.3)}
.ap-meta   {--ac-bg:rgba(255,102,153,.08);--ac-border:rgba(255,102,153,.2);--ac-text:var(--pink);--ac-glow:rgba(255,102,153,.3)}
.ap-scout  {--ac-bg:rgba(0,212,255,.08);--ac-border:rgba(0,212,255,.2);--ac-text:var(--cyan);--ac-glow:rgba(0,212,255,.3)}
.ap-system {--ac-bg:rgba(184,207,224,.06);--ac-border:rgba(184,207,224,.15);--ac-text:var(--text);--ac-glow:rgba(184,207,224,.2)}

/* debate feed */
.deb-feed{
  flex:1;overflow-y:auto;padding:20px 28px;
  display:flex;flex-direction:column;gap:14px;
}
.deb-feed::-webkit-scrollbar{width:4px}
.deb-feed::-webkit-scrollbar-track{background:transparent}
.deb-feed::-webkit-scrollbar-thumb{background:var(--border2);border-radius:2px}

/* event cards */
.ev{animation:slideUp .3s ease both}
@keyframes slideUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

/* market question card */
.ev-market{
  background:linear-gradient(135deg,var(--bg3),var(--bg4));
  border:1px solid var(--border3);border-radius:10px;padding:18px 20px;
}
.ev-market-label{font-size:10px;color:var(--cyan);text-transform:uppercase;
  letter-spacing:.1em;margin-bottom:8px;font-weight:600}
.ev-market-q{font-family:var(--fs);font-size:18px;font-weight:300;
  color:var(--bright);line-height:1.4;font-style:italic}
.ev-market-price{margin-top:10px;font-size:12px;color:var(--dim)}
.ev-market-price span{color:var(--amber);font-weight:600}

/* round badge */
.ev-round{
  display:flex;align-items:center;gap:10px;padding:6px 0;
}
.ev-round::before,.ev-round::after{content:'';flex:1;height:1px;background:var(--border)}
.ev-round-txt{font-size:10px;color:var(--dim);text-transform:uppercase;
  letter-spacing:.12em;white-space:nowrap;padding:0 8px}

/* speech bubble */
.ev-speech{display:flex;gap:14px;align-items:flex-start}
.ev-speech.right{flex-direction:row-reverse}
.agent-avatar{
  width:38px;height:38px;border-radius:10px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:16px;border:1px solid;font-family:var(--fm);
}
.av-bull  {background:rgba(0,229,160,.1);border-color:rgba(0,229,160,.3);color:var(--green)}
.av-bear  {background:rgba(255,51,85,.1);border-color:rgba(255,51,85,.3);color:var(--red)}
.av-devil {background:rgba(176,138,255,.1);border-color:rgba(176,138,255,.3);color:var(--purple)}
.av-judge {background:rgba(255,192,68,.1);border-color:rgba(255,192,68,.3);color:var(--amber)}
.av-meta  {background:rgba(255,102,153,.1);border-color:rgba(255,102,153,.3);color:var(--pink)}
.av-scout {background:rgba(0,212,255,.1);border-color:rgba(0,212,255,.3);color:var(--cyan)}
.av-system{background:rgba(184,207,224,.06);border-color:rgba(184,207,224,.15);color:var(--dim)}

.bubble-wrap{flex:1;max-width:72%}
.ev-speech.right .bubble-wrap{align-items:flex-end;display:flex;flex-direction:column}
.bubble-agent{font-size:10px;font-weight:600;text-transform:uppercase;
  letter-spacing:.08em;margin-bottom:5px}
.bull-lbl{color:var(--green)}.bear-lbl{color:var(--red)}
.devil-lbl{color:var(--purple)}.judge-lbl{color:var(--amber)}
.meta-lbl{color:var(--pink)}.scout-lbl{color:var(--cyan)}
.bubble{
  background:var(--bg3);border:1px solid var(--border2);
  border-radius:10px;padding:13px 16px;
  font-size:12px;line-height:1.7;color:var(--text);
  position:relative;
}
.ev-speech:not(.right) .bubble{border-top-left-radius:2px}
.ev-speech.right .bubble{border-top-right-radius:2px}
.bull-bubble  {border-color:rgba(0,229,160,.25);background:rgba(0,229,160,.04)}
.bear-bubble  {border-color:rgba(255,51,85,.25);background:rgba(255,51,85,.04)}
.devil-bubble {border-color:rgba(176,138,255,.25);background:rgba(176,138,255,.04)}
.judge-bubble {border-color:rgba(255,192,68,.25);background:rgba(255,192,68,.04)}
.meta-bubble  {border-color:rgba(255,102,153,.25);background:rgba(255,102,153,.04)}
.scout-bubble {border-color:rgba(0,212,255,.25);background:rgba(0,212,255,.04)}

/* typewriter cursor */
.bubble.typing::after{content:'▋';animation:blink .7s infinite;color:var(--dim);margin-left:2px}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}

/* thinking indicator */
.ev-thinking{display:flex;gap:14px;align-items:center}
.thinking-dots{display:flex;gap:4px;padding:12px 16px;
  background:var(--bg3);border:1px solid var(--border);border-radius:8px}
.thinking-dots span{width:6px;height:6px;border-radius:50%;background:var(--dim);animation:dotpulse 1.2s infinite}
.thinking-dots span:nth-child(2){animation-delay:.2s}
.thinking-dots span:nth-child(3){animation-delay:.4s}
@keyframes dotpulse{0%,80%,100%{opacity:.2;transform:scale(.8)}40%{opacity:1;transform:scale(1)}}
.thinking-label{font-size:11px;color:var(--dim)}

/* verdict card */
.ev-verdict{
  background:var(--bg3);border:1px solid var(--border2);
  border-radius:10px;padding:18px 20px;
}
.verdict-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.verdict-label{font-size:10px;color:var(--amber);text-transform:uppercase;letter-spacing:.1em;font-weight:600}
.verdict-val{font-family:var(--fh);font-size:28px;font-weight:800;letter-spacing:-1px}
.verdict-yes{color:var(--green)}.verdict-no{color:var(--red)}
.verdict-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.vs-label{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;margin-bottom:3px}
.vs-val{font-size:14px;font-weight:500;color:var(--text)}

/* scout row */
.ev-scout{display:flex;align-items:center;gap:12px;
  padding:10px 14px;background:var(--bg3);border:1px solid var(--border);
  border-radius:8px}
.scout-status{font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px;flex-shrink:0}
.scout-ok  {background:rgba(0,229,160,.1);color:var(--green);border:1px solid rgba(0,229,160,.2)}
.scout-skip{background:rgba(184,207,224,.06);color:var(--dim);border:1px solid var(--border)}
.scout-score{font-size:11px;color:var(--dim);flex-shrink:0}
.scout-score em{color:var(--cyan);font-style:normal;font-weight:600}
.scout-q{font-size:12px;color:var(--text);flex:1;line-height:1.4}

/* trade placed */
.ev-trade{
  background:linear-gradient(135deg,rgba(0,229,160,.08),rgba(0,229,160,.03));
  border:1px solid rgba(0,229,160,.3);border-radius:10px;
  padding:16px 20px;display:flex;align-items:center;gap:14px;
}
.ev-trade-icon{font-size:24px;flex-shrink:0}
.ev-trade-txt{font-size:13px;color:var(--green);font-weight:500;line-height:1.5}

/* no trade */
.ev-notrade{
  background:rgba(184,207,224,.03);border:1px dashed var(--border2);
  border-radius:8px;padding:12px 16px;font-size:12px;color:var(--dim);
  display:flex;align-items:center;gap:10px;
}

/* veto */
.ev-veto{
  background:rgba(255,51,85,.08);border:1px solid rgba(255,51,85,.3);
  border-radius:8px;padding:12px 16px;font-size:12px;color:var(--red);
}

/* bias flag */
.ev-bias{
  background:rgba(255,192,68,.05);border:1px solid rgba(255,192,68,.2);
  border-radius:8px;padding:10px 14px;font-size:11px;color:var(--amber);
}

/* done */
.ev-done{
  text-align:center;padding:24px;
  font-family:var(--fh);font-size:18px;font-weight:700;color:var(--green);
  animation:glow-pulse 2s ease infinite;
}
@keyframes glow-pulse{0%,100%{text-shadow:0 0 20px rgba(0,229,160,.4)}50%{text-shadow:0 0 40px rgba(0,229,160,.8)}}

/* footer */
.deb-footer{
  padding:14px 28px;border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;flex-shrink:0;
}
.deb-footer-info{font-size:11px;color:var(--dim)}
#deb-count{color:var(--cyan)}

/* fade-up for dashboard */
@keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.fu{animation:fadeUp .3s ease both}
.fu:nth-child(2){animation-delay:.05s}
.fu:nth-child(3){animation-delay:.10s}
.fu:nth-child(4){animation-delay:.15s}
.fu:nth-child(5){animation-delay:.20s}
</style>
</head>
<body>
<div class="glow"></div>
<div class="glow2"></div>

<!-- ═══════════════════════════════════════════════════════════
     DEBATE OVERLAY
═══════════════════════════════════════════════════════════ -->
<div id="debate-overlay">
  <div class="deb-header">
    <div>
      <div class="deb-title">Epistemic Debate — Live</div>
      <div style="font-size:11px;color:var(--dim);margin-top:2px">Multi-agent prediction market analysis</div>
    </div>
    <div class="deb-status">
      <svg id="deb-spin" width="14" height="14" viewBox="0 0 14 14" style="animation:spin 1s linear infinite">
        <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--border2)" stroke-width="1.5"/>
        <path d="M7 1.5 A5.5 5.5 0 0 1 12.5 7" fill="none" stroke="var(--cyan)" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <span id="deb-status-txt">Initializing...</span>
      <button class="btn btn-ghost" onclick="closeDebate()" style="padding:6px 12px;font-size:11px">✕ Close</button>
    </div>
  </div>

  <!-- Agent roster -->
  <div class="agents-bar">
    <div class="agent-pill ap-scout"  id="pill-scout">  <div class="dot"></div> Scout</div>
    <div class="agent-pill ap-bull"   id="pill-bull">   <div class="dot"></div> Bull</div>
    <div class="agent-pill ap-bear"   id="pill-bear">   <div class="dot"></div> Bear</div>
    <div class="agent-pill ap-devil"  id="pill-devil">  <div class="dot"></div> Devil's Advocate</div>
    <div class="agent-pill ap-judge"  id="pill-judge">  <div class="dot"></div> Judge</div>
    <div class="agent-pill ap-meta"   id="pill-meta">   <div class="dot"></div> Meta-Judge</div>
  </div>

  <!-- Live feed -->
  <div class="deb-feed" id="deb-feed"></div>

  <div class="deb-footer">
    <div class="deb-footer-info"><span id="deb-count">0</span> events · auto-scrolling</div>
    <button class="btn btn-ghost" onclick="closeDebate()" style="font-size:11px">Close panel</button>
  </div>
</div>

<style>
@keyframes spin{to{transform:rotate(360deg)}}
</style>

<!-- ═══════════════════════════════════════════════════════════
     MAIN DASHBOARD
═══════════════════════════════════════════════════════════ -->
<main>
  <header>
    <div>
      <div class="logo-title">Epistemic <em>Organism</em></div>
      <div class="logo-sub">Paper Trader · Polymarket · v2</div>
    </div>
    <div class="hdr-right">
      <span class="ts" id="ts">—</span>
      <button class="btn btn-ghost" onclick="loadData()">↻ Refresh</button>
      <button class="btn btn-primary" id="scan-btn" onclick="startScan()">⚡ Run Scan</button>
    </div>
  </header>

  <div class="kpis" id="kpis">
    <div class="shimmer" style="height:85px;border-radius:10px"></div>
    <div class="shimmer" style="height:85px;border-radius:10px"></div>
    <div class="shimmer" style="height:85px;border-radius:10px"></div>
    <div class="shimmer" style="height:85px;border-radius:10px"></div>
    <div class="shimmer" style="height:85px;border-radius:10px"></div>
  </div>

  <div class="grid2">
    <div class="panel fu">
      <div class="panel-head">
        <div class="panel-title">Open Positions</div>
        <div class="badge" id="open-badge">—</div>
      </div>
      <div class="panel-body" id="positions"></div>
    </div>
    <div class="panel fu">
      <div class="panel-head">
        <div class="panel-title">Epistemic State</div>
      </div>
      <div class="panel-body" id="epistemic"></div>
    </div>
  </div>

  <div class="panel fu">
    <div class="panel-head">
      <div class="panel-title">Trade History</div>
      <div class="badge" id="history-badge">—</div>
    </div>
    <div id="history"></div>
  </div>
</main>

<script>
// ── Helpers ──────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt  = n => (n<0?'-':'')+'$'+Math.abs(n).toFixed(2);
const fmtp = n => (n>=0?'+':'')+n.toFixed(1)+'%';
const pc   = n => n>=0?'pos':'neg';

// ── Dashboard data ────────────────────────────────────────────────
async function loadData(){
  try{
    const d = await fetch('/api/portfolio').then(r=>r.json());
    renderDashboard(d);
  }catch(e){console.error(e)}
}

function renderDashboard(d){
  $('ts').textContent = 'Updated '+new Date(d.updated).toLocaleTimeString();

  // KPIs
  const rc = d.ret_pct>=0?'pos':'neg';
  const pu = d.total_unreal>=0?'pos':'neg';
  $('kpis').innerHTML = `
    <div class="kpi c-cyan fu"><div class="kpi-lbl">Net Worth (Live MTM)</div>
      <div class="kpi-val">$${d.net_worth.toFixed(2)}</div>
      <div class="kpi-sub">Started $${d.starting.toFixed(2)}</div></div>
    <div class="kpi ${d.ret_pct>=0?'c-green':'c-red'} fu"><div class="kpi-lbl">Total Return</div>
      <div class="kpi-val ${rc}">${fmtp(d.ret_pct)}</div>
      <div class="kpi-sub">PnL ${fmt(d.total_pnl)}</div></div>
    <div class="kpi fu"><div class="kpi-lbl">Cash</div>
      <div class="kpi-val">$${d.bankroll.toFixed(2)}</div>
      <div class="kpi-sub">Deployed $${d.total_cost.toFixed(2)}</div></div>
    <div class="kpi ${d.total_unreal>=0?'c-green':'c-red'} fu"><div class="kpi-lbl">Unrealised PnL</div>
      <div class="kpi-val ${pu}">${fmt(d.total_unreal)}</div>
      <div class="kpi-sub">Tokens MTM $${d.total_mtm.toFixed(2)}</div></div>
    <div class="kpi c-purple fu"><div class="kpi-lbl">Win Rate</div>
      <div class="kpi-val">${d.win_rate}%</div>
      <div class="kpi-sub">${d.won_count}W · ${d.lost_count}L · ${d.open_count} open</div></div>`;

  $('open-badge').textContent = d.open_count+' open';

  // Positions
  const pb = $('positions');
  if(!d.positions.length){pb.innerHTML='<div class="empty">No open positions</div>'}
  else pb.innerHTML = d.positions.map(p=>{
    const tc = p.side==='YES'?'yes-tag':'no-tag';
    const uc = p.unreal>=0?'pos':'neg';
    const bp = p.token_price!=null?(p.token_price*100).toFixed(0):50;
    return `<div class="pos">
      <div class="pos-top">
        <div class="pos-q">${p.question}</div>
        <div class="pos-tag ${tc}">${p.side}</div>
      </div>
      <div class="pos-nums">
        <div><div class="pn-l">Cost</div><div class="pn-v">$${p.size_usdc.toFixed(2)}</div></div>
        <div><div class="pn-l">Bought @</div><div class="pn-v">${p.entry_price.toFixed(4)}</div></div>
        <div><div class="pn-l">Now @</div><div class="pn-v">${p.price_str}</div></div>
        <div><div class="pn-l">MTM Value</div><div class="pn-v">$${p.mtm_val.toFixed(2)}</div></div>
      </div>
      <div class="pos-bar-wrap">
        <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--dim);margin-bottom:4px">
          <span>Unrealised PnL</span>
          <span class="pn-v ${uc}" style="font-size:12px">${p.unreal>=0?'+':''}$${p.unreal.toFixed(2)}</span>
        </div>
        <div class="pos-bar"><div class="pos-bar-fill" style="width:${bp}%"></div></div>
      </div>
    </div>`;
  }).join('');

  // Epistemic
  const w = d.weights;
  const bp2 = (w.bull*100).toFixed(0), be2=(w.bear*100).toFixed(0);
  $('epistemic').innerHTML = `
    <div class="wbar-wrap">
      <div class="wbar-top"><span>🐂 Bull credibility</span><span>${bp2}%</span></div>
      <div class="wbar"><div class="wbar-fill-bull" style="width:${bp2}%"></div></div>
    </div>
    <div class="wbar-wrap">
      <div class="wbar-top"><span>🐻 Bear credibility</span><span>${be2}%</span></div>
      <div class="wbar"><div class="wbar-fill-bear" style="width:${be2}%"></div></div>
    </div>
    <div style="height:1px;background:var(--border);margin:16px 0"></div>
    <div style="font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px">Learned Lessons</div>
    ${d.lessons.length?d.lessons.map((l,i)=>`<div class="lesson">
      <div class="lesson-n">Lesson ${i+1}</div>${l}</div>`).join('')
    :'<div class="empty" style="padding:10px 0">No lessons yet — run resolve after markets close</div>'}`;

  // History
  $('history-badge').textContent = d.closed.length+' closed';
  if(!d.closed.length){$('history').innerHTML='<div class="empty">No closed trades yet</div>'}
  else $('history').innerHTML = `<table class="ttable">
    <thead><tr><th>#</th><th>Side</th><th>Question</th><th>Stake</th><th>Entry</th><th>PnL</th><th>Result</th></tr></thead>
    <tbody>${d.closed.map(t=>{
      const won=t.status==='won';
      const q=t.question.length>65?t.question.slice(0,65)+'…':t.question;
      return `<tr>
        <td style="color:var(--dim)">${t.id}</td>
        <td><span class="pos-tag ${t.side==='YES'?'yes-tag':'no-tag'}">${t.side}</span></td>
        <td style="max-width:280px;line-height:1.5;font-size:12px">${q}</td>
        <td>$${t.size_usdc.toFixed(2)}</td>
        <td style="color:var(--dim)">${t.entry_price.toFixed(4)}</td>
        <td class="pn-v ${won?'pos':'neg'}">${t.pnl!=null?(t.pnl>=0?'+':'')+'$'+Math.abs(t.pnl).toFixed(2):'—'}</td>
        <td><span class="${won?'bwon':'blost'}">${won?'WON':'LOST'}</span></td>
      </tr>`;
    }).join('')}</tbody></table>`;
}

// ── Agent emojis & labels ────────────────────────────────────────
const AGENTS = {
  bull:   {emoji:'🐂', label:'Bull',            cls:'bull',   lbl:'bull-lbl',   bub:'bull-bubble',   av:'av-bull'},
  bear:   {emoji:'🐻', label:'Bear',            cls:'bear',   lbl:'bear-lbl',   bub:'bear-bubble',   av:'av-bear'},
  devil:  {emoji:'😈', label:"Devil's Advocate",cls:'devil',  lbl:'devil-lbl',  bub:'devil-bubble',  av:'av-devil'},
  judge:  {emoji:'⚖️', label:'Judge',           cls:'judge',  lbl:'judge-lbl',  bub:'judge-bubble',  av:'av-judge'},
  meta:   {emoji:'🔍', label:'Meta-Judge',      cls:'meta',   lbl:'meta-lbl',   bub:'meta-bubble',   av:'av-meta'},
  scout:  {emoji:'🛰', label:'Scout',           cls:'scout',  lbl:'scout-lbl',  bub:'scout-bubble',  av:'av-scout'},
  system: {emoji:'⚙️', label:'System',          cls:'system', lbl:'',           bub:'',              av:'av-system'},
};

// right-aligned agents
const RIGHT_AGENTS = new Set(['bear','devil','judge','meta']);

let eventCount = 0;
let currentThinkingEl = null;

function setAgentActive(agent, isThinking=false){
  document.querySelectorAll('.agent-pill').forEach(p=>{
    p.classList.remove('active','thinking');
  });
  const pill = $('pill-'+agent);
  if(pill){
    pill.classList.add('active');
    if(isThinking) pill.classList.add('thinking');
  }
}

function addEvent(html){
  const feed = $('deb-feed');
  const div  = document.createElement('div');
  div.innerHTML = html;
  feed.appendChild(div.firstElementChild || div);
  feed.scrollTop = feed.scrollHeight;
  eventCount++;
  $('deb-count').textContent = eventCount;
}

function removeThinking(){
  if(currentThinkingEl){
    currentThinkingEl.remove();
    currentThinkingEl = null;
  }
}

// Typewriter effect for speech bubbles
function typeText(el, text, speed=12){
  el.classList.add('typing');
  let i=0;
  const tick = () => {
    if(i<text.length){
      el.textContent += text[i++];
      el.closest('.deb-feed').scrollTop = 99999;
      setTimeout(tick, speed + Math.random()*8);
    } else {
      el.classList.remove('typing');
    }
  };
  setTimeout(tick, 0);
}

function renderEvent(evt){
  const A = AGENTS;

  if(evt.type==='start' || evt.type==='info'){
    if(!evt.msg || evt.msg.trim()==='') return;
    // Only show important info lines
    const msg = evt.msg.trim();
    if(msg.startsWith('[v2]') || msg.startsWith('Fetching') ||
       msg.startsWith('Running') || msg.startsWith('Persona') ||
       msg.startsWith('Inject') || msg.startsWith('Found') ||
       msg.startsWith('Market') || msg.startsWith('--')){
      // skip noisy lines
      return;
    }
    addEvent(`<div class="ev ev-bias" style="font-size:11px;color:var(--dim)">ℹ ${msg}</div>`);
    return;
  }

  if(evt.type==='market'){
    removeThinking();
    addEvent(`<div class="ev ev-market">
      <div class="ev-market-label">Debating Market</div>
      <div class="ev-market-q">${evt.question}</div>
    </div>`);
    return;
  }

  if(evt.type==='round'){
    removeThinking();
    addEvent(`<div class="ev ev-round">
      <div class="ev-round-txt">Round ${evt.round}</div>
    </div>`);
    return;
  }

  if(evt.type==='thinking'){
    removeThinking();
    const ag = A[evt.agent] || A.system;
    setAgentActive(evt.agent, true);
    const feed = $('deb-feed');
    const wrap = document.createElement('div');
    wrap.className = 'ev ev-thinking';
    const isRight = RIGHT_AGENTS.has(evt.agent);
    wrap.innerHTML = `
      ${isRight?'<div style="flex:1"></div>':''}
      <div class="agent-avatar ${ag.av}">${ag.emoji}</div>
      <div>
        <div class="bubble-agent ${ag.lbl}" style="font-size:10px;margin-bottom:4px">${ag.label}</div>
        <div class="thinking-dots">
          <span></span><span></span><span></span>
        </div>
        <div class="thinking-label" style="margin-top:4px;font-size:10px;color:var(--dim)">${evt.label}</div>
      </div>
      ${!isRight?'<div style="flex:1"></div>':''}`;
    feed.appendChild(wrap);
    feed.scrollTop = feed.scrollHeight;
    currentThinkingEl = wrap;
    return;
  }

  if(evt.type==='speech'){
    removeThinking();
    const ag  = A[evt.agent] || A.system;
    const txt = evt.text || '';
    const right = RIGHT_AGENTS.has(evt.agent);
    setAgentActive(evt.agent, false);

    // Trim very long texts for display
    const display = txt.length > 600 ? txt.slice(0,600)+'…' : txt;

    const feed = $('deb-feed');
    const wrap = document.createElement('div');
    wrap.className = `ev ev-speech${right?' right':''}`;
    wrap.innerHTML = `
      <div class="agent-avatar ${ag.av}">${ag.emoji}</div>
      <div class="bubble-wrap">
        <div class="bubble-agent ${ag.lbl}">${ag.label}</div>
        <div class="bubble ${ag.bub}" id="bubble-${eventCount}"></div>
      </div>`;
    feed.appendChild(wrap);
    feed.scrollTop = feed.scrollHeight;
    eventCount++;
    $('deb-count').textContent = eventCount;

    // Typewriter
    const bubbleEl = wrap.querySelector('.bubble');
    typeText(bubbleEl, display, 8);
    return;
  }

  if(evt.type==='verdict'){
    removeThinking();
    setAgentActive('judge', false);
    const cls = evt.verdict==='YES'?'verdict-yes':'verdict-no';
    addEvent(`<div class="ev ev-verdict">
      <div class="verdict-header">
        <div class="verdict-label">⚖️ Judge Verdict</div>
        <div class="verdict-val ${cls}">${evt.verdict}</div>
      </div>
    </div>`);
    return;
  }

  if(evt.type==='stat'){
    // Accumulate stats and attach to last verdict card or show inline
    const feed = $('deb-feed');
    let verdict = feed.querySelector('.ev-verdict:last-of-type .verdict-stats');
    if(!verdict){
      // Create stats row under last verdict
      const lastVerdict = feed.querySelector('.ev-verdict:last-of-type');
      if(lastVerdict){
        const statsDiv = document.createElement('div');
        statsDiv.className = 'verdict-stats';
        lastVerdict.appendChild(statsDiv);
        verdict = statsDiv;
      }
    }
    if(verdict){
      const item = document.createElement('div');
      item.innerHTML = `<div class="vs-label">${evt.key}</div><div class="vs-val">${evt.value}</div>`;
      verdict.appendChild(item);
    }
    return;
  }

  if(evt.type==='meta_adjust'){
    removeThinking();
    setAgentActive('meta', false);
    addEvent(`<div class="ev ev-bias">🔍 ${evt.text}</div>`);
    return;
  }

  if(evt.type==='bias'){
    addEvent(`<div class="ev ev-bias">⚠️ Bias detected: ${evt.text}</div>`);
    return;
  }

  if(evt.type==='veto'){
    removeThinking();
    setAgentActive('meta', false);
    addEvent(`<div class="ev ev-veto">🚫 Meta-Judge VETO: ${evt.reason}</div>`);
    return;
  }

  if(evt.type==='scout'){
    const ok = evt.status==='OK';
    setAgentActive('scout', false);
    addEvent(`<div class="ev ev-scout">
      <span class="scout-status ${ok?'scout-ok':'scout-skip'}">${ok?'✓ OK':'✗ SKIP'}</span>
      <span class="scout-score">score <em>${evt.score}</em></span>
      <span class="scout-q">${evt.question}</span>
    </div>`);
    return;
  }

  if(evt.type==='trade'){
    removeThinking();
    addEvent(`<div class="ev ev-trade">
      <div class="ev-trade-icon">💰</div>
      <div class="ev-trade-txt">${evt.text}</div>
    </div>`);
    return;
  }

  if(evt.type==='notrade'){
    addEvent(`<div class="ev ev-notrade">
      <span style="color:var(--dim)">—</span>
      No trade placed · ${evt.reason}
    </div>`);
    return;
  }

  if(evt.type==='complete' || evt.type==='done'){
    removeThinking();
    document.querySelectorAll('.agent-pill').forEach(p=>p.classList.remove('active','thinking'));
    addEvent(`<div class="ev ev-done">✓ Scan Complete</div>`);
    return;
  }
}

// ── Scan control ─────────────────────────────────────────────────
let scanES = null;

async function startScan(){
  if($('scan-btn').disabled) return;

  // Reset
  eventCount = 0;
  $('deb-count').textContent = '0';
  $('deb-feed').innerHTML = '';
  currentThinkingEl = null;
  $('scan-btn').disabled = true;
  $('deb-status-txt').textContent = 'Running...';
  $('deb-spin').style.animationPlayState = 'running';
  document.querySelectorAll('.agent-pill').forEach(p=>p.classList.remove('active','thinking'));
  $('debate-overlay').classList.add('active');

  await fetch('/api/scan',{method:'POST'});

  scanES = new EventSource('/api/scan-stream');
  scanES.onmessage = e => {
    const evt = JSON.parse(e.data);
    renderEvent(evt);

    if(evt.type==='complete'||evt.type==='error'){
      scanES.close();
      $('scan-btn').disabled = false;
      $('deb-status-txt').textContent = evt.type==='complete'?'Complete':'Error';
      $('deb-spin').style.animationPlayState = 'paused';
      setTimeout(loadData, 2000);
    }
  };
  scanES.onerror = () => {
    scanES.close();
    $('scan-btn').disabled = false;
    $('deb-status-txt').textContent = 'Disconnected';
  };
}

function closeDebate(){
  if(scanES){ scanES.close(); scanES=null; }
  $('debate-overlay').classList.remove('active');
  $('scan-btn').disabled = false;
}

// ── Boot ─────────────────────────────────────────────────────────
loadData();
setInterval(loadData, 30000);
</script>
</body>
</html>"""

# ──────────────────────────────────────────────────────────────────
#  HTTP handler
# ──────────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._send(200, "text/html", HTML.encode())
        elif p == "/api/portfolio":
            try:
                self._send(200, "application/json", json.dumps(build_portfolio()).encode())
            except Exception as e:
                self._send(500, "application/json", json.dumps({"error":str(e)}).encode())
        elif p == "/api/scan-stream":
            self._sse()
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/scan":
            if not _scan_running.is_set():
                # Drain old stale messages safely here before starting new thread
                while not _scan_queue.empty():
                    try: _scan_queue.get_nowait()
                    except queue.Empty: break
                threading.Thread(target=run_scan_background, daemon=True).start()
            self._send(200, "application/json", b'{"ok":true}')
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        
        while True:
            try:
                msg = _scan_queue.get(timeout=55)
                self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
                if msg.get("type") in ("complete","error"):
                    break
            except queue.Empty:
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except: break
            except (BrokenPipeError, ConnectionResetError):
                break

# ──────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, webbrowser
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("", args.port), Handler) as srv:
        url = f"http://localhost:{args.port}"
        print(f"\n  ┌─────────────────────────────────────┐")
        print(f"  │  Epistemic Organism Dashboard        │")
        print(f"  │  {url:<35}│")
        print(f"  │  Ctrl+C to stop                     │")
        print(f"  └─────────────────────────────────────┘\n")
        threading.Timer(0.9, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")
