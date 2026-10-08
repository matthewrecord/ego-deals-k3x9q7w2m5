#!/usr/bin/env python3
"""EGO blowout-deal watcher.

Polls deal feeds, extracts a price from each EGO-related title, computes the
true net harvesting cost per Ah:

    (kit price - estimated resale of non-battery parts) / total Ah

and sends a push notification (ntfy.sh) when it falls under your thresholds.
Also alerts when a source keeps failing, and sends a weekly "still alive" ping,
so a silent breakage can't go unnoticed.

Usage:
    python watcher.py          normal run
    python watcher.py --test   send a test notification and exit
Env:
    NTFY_TOPIC   your private ntfy topic (required unless DRY_RUN=1)
    DRY_RUN=1    print alerts instead of sending, and don't write state
"""
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("EGO_CONFIG", ROOT / "config.yaml"))
STATE_PATH = Path(os.environ.get("EGO_STATE", ROOT / "state.json"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "Mozilla/5.0 (compatible; ego-deal-watcher/1.0)"

PRICE_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
MULTI_AH_RE = re.compile(r"(\d+)\s?[x×]\s?(\d+(?:\.\d+)?)\s?-?ah", re.I)
AH_RE = re.compile(r"(\d+(?:\.\d+)?)\s?-?ah", re.I)


def now():
    return datetime.now(timezone.utc)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"seen": {}, "fail_streak": {}, "last_heartbeat": None}


def save_state(state):
    if DRY_RUN:
        return
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def notify(title, message, link=None, priority="default", tags="zap"):
    if DRY_RUN:
        print(f"[DRY RUN] ({priority}) {title}\n    {message}\n    {link or ''}\n")
        return
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        sys.exit("NTFY_TOPIC is not set")
    headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority, "Tags": tags}
    if link:
        headers["Click"] = link
    r = requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()


def fetch(src):
    url = src["url"]
    if url.startswith("file://"):  # used for local testing
        content = Path(url[7:]).read_bytes()
    else:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        content = r.content
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"unparseable feed: {feed.bozo_exception}")
    return [
        {"title": e.get("title", ""), "link": e.get("link", ""), "id": e.get("id") or e.get("link", "")}
        for e in feed.entries
    ]


NOT_A_PRICE_RE = re.compile(r"\s*(off|discount|savings?|rebate|gift ?card|credit|shipping)\b", re.I)
EGO_RE = re.compile(r"\bego\b", re.I)
BARE_RE = re.compile(
    r"tool[- ]only|bare[- ]tool|\bbare\b|without (a )?batter|no batter|"
    r"batter(?:y|ies)\b[^$]{0,30}\bnot included|not included[^$]{0,20}batter", re.I)


def parse_price(title):
    """First $ amount in the title that isn't a '$20 off' style figure."""
    for m in PRICE_RE.finditer(title):
        if not NOT_A_PRICE_RE.match(title, m.end()):
            return float(m.group(1).replace(",", ""))
    return None


def total_ah(title):
    m = MULTI_AH_RE.search(title)
    if m:
        return int(m.group(1)) * float(m.group(2))
    m = AH_RE.search(title)
    return float(m.group(1)) if m else None


def match_kit(title, kits):
    t = title.lower()
    for kit in kits:
        if any(re.search(p, t) for p in kit["match"]):
            return kit
    return None


def kit_resale(kit, cfg):
    if "resale" in kit:
        return kit["resale"]
    bare = kit.get("bare") or max(0, kit["msrp"] - cfg.get("bare_estimate_per_ah", 28) * kit["ah"])
    return round(cfg.get("resale_factor", 0.65) * bare)


# --- Fallback: identify a kit from a descriptive title with no model number ------------------
CAT_RES = [  # order matters: first hit wins
    ("multihead", re.compile(r"multi.?head")),
    ("zeroturn", re.compile(r"zero.?turn")),
    ("tractor", re.compile(r"lawn tractor|\btractor\b")),
    ("mower", re.compile(r"mower")),
    ("shovel", re.compile(r"snow shovel")),
    ("snow", re.compile(r"snow ?(blower|thrower)")),
    ("pressure", re.compile(r"pressure washer")),
    ("polesaw", re.compile(r"pole ?saw")),
    ("chainsaw", re.compile(r"chain ?saw")),
    ("hedge", re.compile(r"hedge")),
    ("trimmer", re.compile(r"trimmer")),
    ("blower", re.compile(r"blower")),
    ("auger", re.compile(r"earth auger")),
]
AH_ALL = re.compile(r"(?<![\d.])(?:\(?(\d)\)?(?:\s*[x×]\s*|\s+))?(\d+(?:\.\d+)?)\s?-?ah", re.I)
BUNDLE_RE = re.compile(r"\b(extra|additional|bonus|spare)\b")


def title_total_ah(t):
    t = re.sub(r"\btwo\b", "2", re.sub(r"\bthree\b", "3", t))
    total = 0.0
    for m in AH_ALL.finditer(t):
        total += (int(m.group(1)) if m.group(1) else 1) * float(m.group(2))
    return total or None


def title_size(t, cat):
    if cat == "pressure":
        m = re.search(r"(\d{4})\s?psi", t)
    elif cat == "blower":
        m = re.search(r"(\d{3,4})\s?cfm", t)
    else:
        m = re.search(r'(?<![\d.])(\d{2})\s?-?\s?(?:in\b|inch|")', t)
    return int(m.group(1)) if m else None


def title_prop(t, cat):
    if cat in ("mower",):
        if re.search(r"self.?propelled|touch drive|\bsp\b", t):
            return "sp"
        return "push" if "push" in t else None
    if cat == "snow":
        if re.search(r"two.?stage|2.?stage", t):
            return "2"
        if re.search(r"single.?stage|1.?stage", t):
            return "1"
    return None


BATTERY_YES_RE = re.compile(
    r"batter(?:y|ies) (?:included|&|and|,)|(?:with|w/|includes?|including)\s*(?:a |the |\(?\d\)? )?(?:56 ?v(?:olt)? )?batter|"
    r"batteries included|battery,? charger")


def infer_kits(title, cfg):
    """Best-effort catalog candidates for a title with no model number.

    Returns (candidates, ah_assumed). Empty list if unsure. When the title names no Ah but clearly says a
    battery is included, the catalog's own Ah is used, but only if every candidate agrees on it."""
    t = title.lower()
    if BUNDLE_RE.search(t) or BARE_RE.search(t):  # extra-battery bundles / tool-only listings
        return [], False
    cats = [c for c, r in CAT_RES if r.search(t)]
    if not cats:
        return [], False
    cat = "combo" if ("trimmer" in cats and "blower" in cats and cats[0] != "multihead") else cats[0]
    ah = title_total_ah(t)
    assumed = False
    if not ah:
        if not BATTERY_YES_RE.search(t):
            return [], False
        assumed = True
    size, prop = title_size(t, cat), title_prop(t, cat)
    cands = [
        k for k in cfg["kits"]
        if k.get("cat") == cat
        and (assumed or abs(k["ah"] - ah) < 0.05)
        and (size is None or k.get("size") in (None, size))
        and (prop is None or k.get("prop") in (None, prop))
    ]
    if assumed and len({k["ah"] for k in cands}) > 1:
        return [], False  # catalog candidates disagree on battery size: too ambiguous
    for term, tag in ((r"select cut xp|\bxp\b", "xp"), (r"peak power", "peak")):
        if re.search(term, t):
            tagged = [k for k in cands if tag in k.get("tags", [])]
            cands = tagged or cands
    return cands, assumed


def evaluate(title, cfg):
    """Return (label, ah, resale, price, cost_per_ah, inferred) or None if not evaluable.

    `inferred` is None for model-number / battery-only matches, else the list of catalog kit
    names this descriptive title could be (the most conservative one is used for the math)."""
    t = title.lower()
    price = parse_price(title)
    if not price or price <= 0:
        return None

    inferred = None
    kit = match_kit(title, cfg["kits"])
    if not kit and not EGO_RE.search(t):
        return None  # model-number matches don't need the word "ego"; everything else does
    if not kit:
        cands, assumed = infer_kits(title, cfg)
        # a price far below list means a mis-parse (e.g. "$20 off") or a wrong match: skip
        cands = [k for k in cands if price >= 0.35 * k["msrp"]]
        if cands:
            kit = min(cands, key=lambda k: kit_resale(k, cfg))  # highest $/Ah = fewest false alarms
            inferred = [k["name"] + (" [battery size not in title; taken from catalog]" if assumed else "")
                        for k in cands]
    if kit and (BARE_RE.search(t) or price < 0.35 * kit["msrp"]):
        return None  # bare-tool listing, or a price too far below list to be this kit
    if kit:
        ah, label, resale = kit["ah"], kit["name"], kit_resale(kit, cfg)
    elif cfg.get("battery_only", {}).get("enabled", True) and "batter" in t:
        ah = total_ah(title)
        if not ah:
            return None
        # A title that also names a tool is a kit we don't know; skip rather than guess.
        if re.search(cfg["battery_only"].get("exclude_if", r"$^"), t):
            return None
        resale, label = 0, f"Battery-only ({ah:g} Ah)"
    else:
        return None

    return label, ah, resale, price, (price - resale) / ah, inferred


def thresholds_for(ah, cfg):
    """Stricter $/Ah bar for small packs (total capacity under small_battery_thresholds.below_ah)."""
    sb = cfg.get("small_battery_thresholds")
    if sb and ah < sb["below_ah"]:
        return {"strong": sb["strong"], "watch": sb["watch"]}
    return cfg["thresholds"]


REFURB_RE = re.compile(r"refurb|reconditioned|certified|open[- ]box|\bused\b|like new", re.I)


def evaluate_bare(title, cfg):
    """Tools that never ship with batteries: flag listings far below their typical new price.

    Returns None if the title isn't a tracked bare tool, else a dict with a `deep` flag."""
    bt = cfg.get("bare_tools")
    if not bt:
        return None
    t = title.lower()
    price = parse_price(title)
    if not price:
        return None
    for tool in bt["tools"]:
        hit = any(re.search(p, t) for p in tool["match"]) or (
            EGO_RE.search(t) and any(re.search(p, t) for p in tool.get("alt", [])))
        if not hit:
            continue
        ref = tool["ref"]
        pct = tool.get("pct", bt.get("deep_discount_pct", 40))
        if price < 0.2 * ref:  # almost certainly an accessory price or a mis-parse
            return {"deep": False}
        return {"deep": price <= ref * (1 - pct / 100), "name": tool["name"], "price": price,
                "ref": ref, "off": round(100 * (1 - price / ref)), "pct": pct,
                "refurb": bool(REFURB_RE.search(t))}
    return None


def note_unmatched(state, e):
    """Remember EGO listings we couldn't evaluate, so the weekly digest can surface new models."""
    t = e["title"]
    price = parse_price(t)
    if not EGO_RE.search(t) or BARE_RE.search(t) or not price or price < 50:
        return
    um = state.setdefault("unmatched", {})
    if e["id"] not in um and len(um) < 200:
        um[e["id"]] = {"title": t[:140], "price": price, "link": e["link"]}


def tier(cost, th):
    if cost <= th["strong"]:
        return "STRONG"
    if cost <= th["watch"]:
        return "WATCH"
    return None


def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text())
    if "--test" in sys.argv:
        notify("EGO watcher test", "If you can read this, notifications work.", tags="white_check_mark")
        return

    state = load_state()
    th = cfg["thresholds"]
    realert = th.get("realert_if_price_drops_pct", 3) / 100
    ok_sources, evaluated = 0, 0

    for src in cfg["sources"]:
        name = src["name"]
        try:
            entries = fetch(src)
        except Exception as exc:  # noqa: BLE001 - any failure counts as a failed poll
            streak = state["fail_streak"].get(name, 0) + 1
            state["fail_streak"][name] = streak
            print(f"[fail] {name}: {exc} (streak {streak})")
            if streak == cfg.get("fail_alert_after_runs", 12):
                notify(f"EGO watcher: {name} failing",
                       f"{name} has failed {streak} runs in a row. Last error: {exc}",
                       priority="high", tags="warning")
            continue
        ok_sources += 1
        state["fail_streak"][name] = 0

        for e in entries:
            bare = evaluate_bare(e["title"], cfg)
            if bare is not None:
                evaluated += 1
                if bare["deep"]:
                    key, prev = e["id"], state["seen"].get(e["id"])
                    if not prev or bare["price"] < prev["price"] * (1 - realert):
                        state["seen"][key] = {"price": bare["price"], "ts": now().isoformat()}
                        kind = "refurb/used" if bare["refurb"] else "new"
                        notify(
                            f"DEEP DISCOUNT: {bare['name']} ${bare['price']:,.0f} ({bare['off']}% off)",
                            f"{bare['name']}\n${bare['price']:,.0f} vs typical new ${bare['ref']:,.0f}: "
                            f"{bare['off']}% off (alert bar {bare['pct']}%), looks {kind}\n{e['title']}",
                            link=e["link"], priority="high", tags="moneybag")
                continue
            result = evaluate(e["title"], cfg)
            if not result:
                note_unmatched(state, e)
                continue
            evaluated += 1
            label, ah, resale, price, cost, inferred = result
            if inferred and ah < 5:  # low-confidence matches on small packs aren't worth verifying
                continue
            th = thresholds_for(ah, cfg)
            level = tier(cost, th)
            if not level:
                continue
            key = e["id"]
            prev = state["seen"].get(key)
            if prev and price >= prev["price"] * (1 - realert):
                continue
            state["seen"][key] = {"price": price, "ts": now().isoformat()}
            detail = f"${price:,.0f} for {ah:g} Ah"
            if resale:
                detail += f", ${resale:,.0f} est. resale"
            if inferred:  # matched from the description, not a model number
                label = "LOW-CONFIDENCE MATCH (no model number in title): " + " OR ".join(inferred)
                detail += ". Verify the kit contents before buying"
            notify(
                f"{level} EGO deal: ${cost:,.1f}/Ah" + (" (verify kit)" if inferred else ""),
                f"{label}\n{detail}\nNet ${cost:,.1f}/Ah (strong <= ${th['strong']}, watch <= ${th['watch']})\n{e['title']}",
                link=e["link"],
                priority="high" if level == "STRONG" else "default",
                tags="rotating_light" if level == "STRONG" else "zap",
            )

    # prune old alert records
    cutoff = now() - timedelta(days=cfg.get("forget_after_days", 60))
    state["seen"] = {k: v for k, v in state["seen"].items() if datetime.fromisoformat(v["ts"]) > cutoff}

    # weekly heartbeat (also keeps the repo "active" so GitHub doesn't pause the schedule)
    hb = state.get("last_heartbeat")
    due = not hb or now() - datetime.fromisoformat(hb) >= timedelta(days=cfg.get("heartbeat_days", 7))
    if due:
        um = sorted(state.get("unmatched", {}).values(), key=lambda v: -v["price"])[:12]
        msg = f"{ok_sources}/{len(cfg['sources'])} sources OK; {evaluated} EGO listings evaluated this run."
        if um:
            msg += ("\n\nEGO listings seen this week that I couldn't match to the catalog "
                    "(new models? matching gap?):\n" + "\n".join(f"${v['price']:,.0f}  {v['title'][:90]}\n{v['link'].split('?')[0]}" for v in um))
        notify("EGO watcher: weekly digest" if um else "EGO watcher alive", msg,
               priority="low" if um else "min", tags="green_circle")
        state["last_heartbeat"] = now().isoformat()
        state["unmatched"] = {}

    # yearly-ish reminder to refresh catalog prices / add new models
    checked = cfg.get("catalog_checked")
    if checked and (now().date() - date.fromisoformat(str(checked))).days >= cfg.get("catalog_stale_after_days", 365):
        last = state.get("last_stale_notice")
        if not last or now() - datetime.fromisoformat(last) >= timedelta(days=30):
            notify("EGO watcher: refresh your catalog",
                   f"Kit prices in config.yaml were last verified {checked}. EGO reprices and releases new kits, "
                   "so deal math drifts. Re-check list prices and add new models (the weekly digest shows unmatched "
                   "listings), then update catalog_checked.",
                   priority="default", tags="calendar")
            state["last_stale_notice"] = now().isoformat()

    save_state(state)
    print(f"done: {ok_sources}/{len(cfg['sources'])} sources ok, {evaluated} evaluated")


if __name__ == "__main__":
    main()
