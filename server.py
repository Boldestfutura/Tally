#!/usr/bin/env python3
"""Minimal two-player tally, no dependencies. Serves a mobile-first page and a small JSON API."""
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = os.environ.get("TALLY_DATA", "/data/tally.json")
PORT = int(os.environ.get("TALLY_PORT", "8080"))
PLAYERS = ("cameron", "paget")
LABELS = {"cameron": "Cameron", "paget": "Paget"}

APP_VERSION = "2.0.4"
UPDATE_REPO = "Boldestfutura/Tally"
UPDATE_BRANCH = "main"
UPDATE_DIR = os.path.dirname(DATA) or "."
UPDATE_STATUS_FILE = os.path.join(UPDATE_DIR, "update-status.json")
UPDATE_REQUEST_FILE = os.path.join(UPDATE_DIR, "update.req")
UPDATE_CHECK_SECONDS = 30 * 60

# Without this the app simply will not open with no signal, and a scoreboard
# you cannot open on a plane is the whole problem. Shell only - the state comes
# from the queue in localStorage, never from a stale cached response.
SW = """
const VERSION = '%s';
const CACHE = 'tally-' + VERSION;
const SHELL = ['/', '/manifest.webmanifest?v=' + VERSION,
               '/icon-180.png?v=' + VERSION, '/icon-192.png?v=' + VERSION,
               '/icon-512.png?v=' + VERSION, '/icon-512-maskable.png?v=' + VERSION];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Aeroplane wi-fi is the awkward case: it resolves and accepts the connection
// and then never answers, so a plain fetch sits there for half a minute before
// anyone finds out. Nothing here is allowed to take longer than this.
function timed(p, ms) {
  return new Promise((ok, no) => {
    const t = setTimeout(() => no(new Error('slow')), ms);
    p.then(v => { clearTimeout(t); ok(v); }, e => { clearTimeout(t); no(e); });
  });
}

self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  // the API is never cached: a stale score is worse than no score, and the page
  // already knows what it is owed from its own queue
  if (u.pathname.startsWith('/api/')) return;

  // Cache first, not network first. The app has to open now - whether there is
  // a signal is not the user's problem - and a refresh runs behind it so the
  // next open has whatever is current.
  e.respondWith(
    caches.match(e.request).then(hit => {
      const fresh = timed(fetch(e.request), 4000).then(r => {
        if (r && r.ok) {
          const copy = r.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return r;
      });
      if (hit) {
        fresh.catch(() => {});        // best effort; the cached copy already went out
        return hit;
      }
      return fresh.catch(() => caches.match('/'));
    })
  );
});
""" % APP_VERSION

KEEP_MONTHS = 240        # 20 years of results is plenty

_lock = threading.Lock()


def current_period():
    """Calendar month in the container's local timezone (TZ=Australia/Melbourne)."""
    return time.strftime("%Y-%m")


def load():
    try:
        with open(DATA) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}

    state = {p: int(d.get(p, 0) or 0) for p in PLAYERS}
    state["period"] = d.get("period") if isinstance(d.get("period"), str) else None

    # running totals as they stood when the current month began
    raw = d.get("start")
    if not isinstance(raw, dict):
        raw = {}
    state["start"] = {p: int(raw.get(p, 0) or 0) for p in PLAYERS}

    # Which taps have already been counted. A phone with no signal keeps its
    # taps and sends them when it gets one; without this, a retry after a
    # half-failed sync would count them twice.
    seen = {}
    raw_seen = d.get("seen")
    if isinstance(raw_seen, dict):
        for k, v in raw_seen.items():
            if isinstance(k, str) and len(k) <= 64:
                try:
                    seen[k] = int(v)
                except (TypeError, ValueError):
                    pass
    state["seen"] = seen

    months = []
    for m in (d.get("months") or [])[-KEEP_MONTHS:]:
        if not isinstance(m, dict) or not isinstance(m.get("period"), str):
            continue
        try:
            months.append({"period": m["period"],
                           "cameron": int(m.get("cameron", 0)),
                           "paget": int(m.get("paget", 0))})
        except (TypeError, ValueError):
            continue
    state["months"] = months
    return state


def month_points(state):
    """Points scored inside the current month = running total minus the total at month start."""
    return {p: max(0, state[p] - state["start"].get(p, 0)) for p in PLAYERS}


def roll_if_needed(state):
    """Close off the finished month. The running totals keep going; only the month resets."""
    now = current_period()
    was = state.get("period")

    if was is None:                      # first run, or a file written before months existed
        state["period"] = now
        state["start"] = {p: state[p] for p in PLAYERS}
        return True
    if was == now:
        return False

    pts = month_points(state)
    if pts[PLAYERS[0]] or pts[PLAYERS[1]]:        # don't archive a month nobody played
        row = {"period": was, "cameron": pts["cameron"], "paget": pts["paget"]}
        # a month should only ever appear once, even if the clock went backwards
        for i, m in enumerate(state["months"]):
            if m.get("period") == was:
                state["months"][i] = row
                break
        else:
            state["months"].append(row)
        del state["months"][:-KEEP_MONTHS]

    state["start"] = {p: state[p] for p in PLAYERS}   # totals roll on untouched
    state["period"] = now
    return True


def floor_for(state, who):
    return state["start"].get(who, 0)


SEEN_KEEP_DAYS = 21          # far longer than any flight, short enough to stay small
SEEN_MAX = 3000


def apply_events(state, events):
    """Count a batch of taps, skipping any already counted.

    Taps are deltas rather than totals, which is what makes this safe: two
    phones that were both offline can each send their own and both land. An id
    per tap is what makes a retry safe."""
    seen = state.setdefault("seen", {})
    now = int(time.time())
    accepted = []
    for e in list(events)[:300]:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        if not re.match(r"^[A-Za-z0-9_-]{8,64}$", eid):
            continue
        who = str(e.get("who") or "")
        if who not in PLAYERS:
            continue
        try:
            delta = int(e.get("delta", 0))
        except (TypeError, ValueError):
            continue
        delta = max(-1, min(1, delta))
        if delta == 0:
            continue
        if eid in seen:
            accepted.append(eid)      # already counted; tell the phone to forget it
            continue
        state[who] = max(floor_for(state, who), state[who] + delta)
        seen[eid] = now
        accepted.append(eid)

    cutoff = now - SEEN_KEEP_DAYS * 86400
    for k in [k for k, v in seen.items() if v < cutoff]:
        del seen[k]
    if len(seen) > SEEN_MAX:
        for k in sorted(seen, key=seen.get)[:len(seen) - SEEN_MAX]:
            del seen[k]
    return accepted


def for_client(state):
    out = {p: state[p] for p in PLAYERS}
    out["period"] = state.get("period")
    out["month"] = month_points(state)
    out["months"] = state.get("months", [])
    return out


def save(state):
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DATA))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, DATA)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _version_tuple(value):
    try:
        return tuple(int(part) for part in str(value).strip().lstrip("v").split("."))
    except (TypeError, ValueError):
        return ()


def update_status():
    try:
        with open(UPDATE_STATUS_FILE) as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return {"current": APP_VERSION, "latest": d.get("latest") or APP_VERSION,
                    "available": bool(d.get("available")),
                    "state": d.get("state") or "idle",
                    "message": d.get("message") or "",
                    "checked": d.get("checked")}
    except (OSError, ValueError):
        pass
    return {"current": APP_VERSION, "latest": APP_VERSION,
            "available": False, "state": "checking", "message": "Checking GitHub"}


def write_update_status(state, latest=None, message="", available=False, checked=None):
    os.makedirs(UPDATE_DIR, exist_ok=True)
    row = {"current": APP_VERSION, "latest": latest or APP_VERSION,
           "available": bool(available), "state": state, "message": message,
           "checked": int(checked or time.time())}
    fd, tmp = tempfile.mkstemp(dir=UPDATE_DIR, prefix=".update-status-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(row, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, UPDATE_STATUS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return row


def check_update():
    """Compare VERSION at GitHub's current branch commit with this running copy."""
    try:
        api = "https://api.github.com/repos/%s/commits/%s" % (UPDATE_REPO, UPDATE_BRANCH)
        req = urllib.request.Request(api, headers={"User-Agent": "Tally-updater",
                                                    "Accept": "application/vnd.github+json",
                                                    "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=15) as r:
            sha = json.loads(r.read(1 << 20)).get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("GitHub did not return a commit id")
        url = "https://raw.githubusercontent.com/%s/%s/VERSION" % (UPDATE_REPO, sha)
        req = urllib.request.Request(url, headers={"User-Agent": "Tally-updater",
                                                    "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=15) as r:
            latest = r.read(128).decode("ascii", "replace").strip()
        if not re.match(r"^\d+\.\d+\.\d+$", latest):
            raise ValueError("GitHub VERSION is not a stable x.y.z version")
        available = _version_tuple(latest) > _version_tuple(APP_VERSION)
        state = "available" if available else "current"
        message = "Update available" if available else "Tally is up to date"
        return write_update_status(state, latest, message, available)
    except Exception as e:
        old = update_status()
        return write_update_status("error", old.get("latest"),
                                  "Could not check GitHub: %s" % str(e)[:120],
                                  old.get("available", False))


def update_check_loop():
    while True:
        check_update()
        time.sleep(UPDATE_CHECK_SECONDS)


def request_update():
    status = update_status()
    if not status.get("available"):
        status = check_update()
    if not status.get("available"):
        return False, status.get("message") or "Tally is up to date"
    try:
        os.makedirs(UPDATE_DIR, exist_ok=True)
        if os.path.exists(UPDATE_REQUEST_FILE):
            return True, "update already queued"
        with open(UPDATE_REQUEST_FILE, "x") as fh:
            json.dump({"requested": int(time.time()), "version": status.get("latest")}, fh)
        write_update_status("queued", status.get("latest"), "Update queued on the NAS", True)
    except FileExistsError:
        return True, "update already queued"
    except OSError as e:
        return False, "could not queue update: %s" % str(e)[:120]
    return True, "update queued; Tally will restart when it is installed"


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#edf3ff" media="(prefers-color-scheme: light)" id="themeLight">
<meta name="theme-color" content="#090f1d" media="(prefers-color-scheme: dark)" id="themeDark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Tally">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png?v=__APP_VERSION__">
<link rel="apple-touch-icon" sizes="180x180" href="/icon-180.png?v=__APP_VERSION__">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png?v=__APP_VERSION__">
<link rel="manifest" href="/manifest.webmanifest?v=__APP_VERSION__">
<title>Tally</title>
<style>
  :root{
    color-scheme: light dark;
    --bg:#f7f8fa;
    --tint-a:47,109,246; --tint-b:249,115,22; --tint-base:.105;
    --ta:1; --tb:1;
    --card:rgba(255,255,255,.82);
    --card-brd:rgba(16,20,28,.09);
    --shadow:0 1px 2px rgba(16,20,28,.05), 0 12px 32px -12px rgba(16,20,28,.16);
    --ink:#11141a;
    --muted:#687083;
    --faint:rgba(16,20,28,.055);
    --a:#2f6df6; --a-soft:rgba(47,109,246,.12);
    --b:#f97316; --b-soft:rgba(249,115,22,.13);
    --ring:rgba(47,109,246,.35);
    --alert:#e11d48;
    --radius:22px;
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0b0d11;
      --tint-a:91,140,255; --tint-b:255,140,58; --tint-base:.16;
      --card:rgba(24,28,35,.72);
      --card-brd:rgba(255,255,255,.08);
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 18px 40px -16px rgba(0,0,0,.65);
      --ink:#f1f3f7;
      --muted:#99a1b0;
      --faint:rgba(255,255,255,.06);
      --a:#5b8cff; --a-soft:rgba(91,140,255,.16);
      --b:#ff8c3a; --b-soft:rgba(255,140,58,.18);
      --ring:rgba(91,140,255,.45);
      --alert:#ff4d6d;
    }
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{height:100%}
  body{
    margin:0;
    padding:calc(env(safe-area-inset-top,0px) + 10px) 0 calc(env(safe-area-inset-bottom,0px) + 14px);
    background:var(--bg);
    color:var(--ink);
    font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif;
    -webkit-text-size-adjust:100%;
    -webkit-font-smoothing:antialiased;
  }
  /* lead indicator: two fixed washes whose OPACITY is animated, which the compositor
     can do on its own thread. Animating the gradient itself repainted the whole page. */
  .tint{position:fixed;inset:0;z-index:0;pointer-events:none;
        opacity:0;transition:opacity .9s ease;will-change:opacity;transform:translateZ(0)}
  .tint.a{background:radial-gradient(135% 95% at 6% -10%,
            rgba(var(--tint-a), var(--tint-base)) 0%, transparent 62%)}
  .tint.b{background:radial-gradient(135% 95% at 94% -6%,
            rgba(var(--tint-b), var(--tint-base)) 0%, transparent 62%)}

  .wrap{position:relative;z-index:1;max-width:760px;margin:0 auto;padding:0 16px;
        display:flex;flex-direction:column;min-height:100%}
  header{display:flex;align-items:center;justify-content:space-between;
         gap:12px;padding:6px 2px 18px}
  .h-left{display:flex;align-items:center;gap:10px;min-width:0}
  .h-right{display:flex;align-items:center;gap:10px;flex:0 0 auto}
  .logo{display:block;flex:0 0 auto}
  .logo .sa{stroke:var(--a)}
  .logo .sb{stroke:var(--b)}
  .logo .slash{stroke:var(--ink);opacity:.62}
  .titles{display:flex;flex-direction:column;gap:1px;min-width:0}
  .brand{font-size:14px;font-weight:650;letter-spacing:.2em;text-transform:uppercase;
         color:var(--muted);margin:0}
  .period{font-size:12.5px;font-weight:600;color:var(--ink);margin:0;opacity:.75}
  .sync{font-size:11.5px;color:var(--muted);display:flex;align-items:center;gap:6px}
  .dot{width:7px;height:7px;border-radius:50%;background:#35c07a;flex:0 0 auto;transition:background .2s}
  .sync[data-off="1"] .dot{background:#e0a32e}
  .sync[data-off="1"]{color:#e0a32e}

  .life{display:flex;flex-direction:column;align-items:center;gap:3px;margin:-2px 0 15px}
  .life .cap{font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;
             color:var(--muted);font-weight:650}
  .life .vals{display:flex;align-items:baseline;gap:12px;
              font-variant-numeric:tabular-nums;letter-spacing:-.02em}
  .life b{font-size:27px;font-weight:750;line-height:1}
  .life .lc{color:var(--a)}
  .life .lp{color:var(--b)}
  .life .sep{color:var(--muted);font-size:15px;font-weight:500}

  .grid{display:grid;grid-template-columns:1fr;gap:12px}
  @media (min-width:640px){ .grid{grid-template-columns:1fr 1fr} }

  .card{
    position:relative;overflow:hidden;
    background:var(--card);
    -webkit-backdrop-filter:blur(18px) saturate(140%);
    backdrop-filter:blur(18px) saturate(140%);
    border:1px solid var(--card-brd);
    border-radius:var(--radius);
    box-shadow:var(--shadow);
    padding:14px 15px 13px;
    text-align:center;
    transition:transform .25s ease;
  }
  /* steady ring in the player's colour; only the 10-behind alert pulses */
  .card{box-shadow:var(--shadow), 0 0 0 1.6px var(--accent), 0 6px 18px -9px var(--accent)}
  /* 10 or more behind: the name pulses red. opacity only, so it runs on the compositor. */
  .card[data-trail="1"] .name{color:var(--alert);animation:namePulse 1.5s ease-in-out infinite}
  @keyframes namePulse{0%,100%{opacity:1}50%{opacity:.35}}
  .card[data-who="cameron"]{--accent:var(--a);--accent-soft:var(--a-soft)}
  .card[data-who="paget"]{--accent:var(--b);--accent-soft:var(--b-soft)}

  .name{display:flex;align-items:center;justify-content:center;gap:8px;
        font-size:14px;font-weight:650;letter-spacing:.02em;margin:4px 0 0;color:var(--accent);
        transition:color .35s ease;will-change:opacity}

  .score{
    position:relative;overflow:hidden;
    font-size:clamp(54px,17.5vw,78px);line-height:1;font-weight:700;
    letter-spacing:-.045em;font-variant-numeric:tabular-nums;
    margin:7px 0 3px;color:var(--ink);
  }
  .score .n{display:block;will-change:transform,opacity;backface-visibility:hidden}
  .score .ghost{position:absolute;left:0;right:0;top:0}
  .pts{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:0 0 11px}

  .row{display:flex;gap:9px}
  button{
    font:inherit;cursor:pointer;border-radius:15px;border:1px solid transparent;
    padding:14px 0;flex:1;font-weight:650;
    touch-action:manipulation;user-select:none;
    transition:transform .08s ease, opacity .15s ease;
  }
  button:active{transform:scale(.96)}
  button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--ring)}
  .add{flex:1;font-size:21px;color:#fff;background:var(--accent);
       box-shadow:0 6px 16px -8px var(--accent)}
  .add:hover{filter:brightness(1.06)}
  .sub{flex:1;font-size:21px;color:var(--muted);background:var(--faint);border-color:var(--card-brd)}
  .sub:disabled{opacity:.35;cursor:default;transform:none}

  /* burst particles — fixed to the viewport so the card's overflow can't clip them */
  .fx{position:fixed;pointer-events:none;z-index:60;
      will-change:transform,opacity;backface-visibility:hidden;contain:layout style paint}
  .fx.conf{width:7px;height:10px;border-radius:2px}
  .fx.puff{width:5px;height:5px;border-radius:50%}
  .fx.rain{width:9px;height:14px;border-radius:2px;opacity:0;z-index:70}

  /* milestone takeover at every multiple of 10 */
  .cheer{position:fixed;inset:0;z-index:80;pointer-events:none;
         display:flex;align-items:flex-start;justify-content:center;
         padding:calc(env(safe-area-inset-top,0px) + 14px) 18px 0}
  .cheer b{
    will-change:transform,opacity;
    font-size:clamp(21px,6.2vw,34px);font-weight:750;letter-spacing:-.02em;
    text-align:center;line-height:1.15;max-width:17ch;
    padding:12px 20px;border-radius:18px;
    background:var(--card);border:1px solid var(--card-brd);
    -webkit-backdrop-filter:blur(20px) saturate(150%);
    backdrop-filter:blur(20px) saturate(150%);
    box-shadow:0 10px 34px -10px rgba(0,0,0,.42);
    animation:cheer 2.4s cubic-bezier(.2,1.35,.4,1) forwards;
  }
  @keyframes cheer{
    0%{opacity:0;transform:scale(.72)}
    16%{opacity:1;transform:scale(1.07)}
    28%{transform:scale(1)}
    72%{opacity:1}
    100%{opacity:0;transform:scale(1.03)}
  }

  .pop{position:absolute;left:50%;top:44%;transform:translate(-50%,0);
       font-size:22px;font-weight:700;color:var(--accent);pointer-events:none;
       animation:pop .75s ease-out forwards}
  @keyframes pop{
    0%{opacity:0;transform:translate(-50%,6px) scale(.8)}
    22%{opacity:1}
    100%{opacity:0;transform:translate(-50%,-52px) scale(1.05)}
  }


  .lead{text-align:center;font-size:13px;color:var(--muted);margin:3px 0 0;min-height:1.25em;
        transition:opacity .2s}
  .nudge{text-align:center;font-size:14.5px;font-weight:600;margin:8px 0 0;min-height:1.3em;
         letter-spacing:-.005em}
  .nudge span{display:inline-block;animation:nudgeIn .42s cubic-bezier(.2,.9,.3,1)}
  @keyframes nudgeIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}

  /* past months */
  .hist{margin:22px 0 4px;background:var(--card);border:1px solid var(--card-brd);
        border-radius:18px;overflow:hidden;box-shadow:var(--shadow)}
  .hist-h{display:flex;align-items:baseline;justify-content:space-between;gap:8px;
          flex-wrap:wrap;padding:12px 15px 10px;border-bottom:1px solid var(--card-brd)}
  .hist-h b{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);font-weight:650}
  .tally-sum{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
  .tally-sum i{font-style:normal;font-weight:700}
  .mrow{display:flex;align-items:center;gap:10px;padding:11px 15px;
        border-bottom:1px solid var(--faint);font-size:14px}
  .mrow:last-child{border-bottom:none}
  .mrow.live{background:var(--faint)}
  .mrow.live .mon em{font-style:normal;font-weight:500;color:var(--muted);font-size:12px}
  .mrow.live .win{opacity:.85}
  .mrow .mon{font-weight:600;min-width:0;flex:1 1 auto}
  .mrow .sc{font-variant-numeric:tabular-nums;color:var(--muted);font-size:13px;flex:0 0 auto}
  .mrow .win{flex:0 0 auto;font-size:11.5px;font-weight:700;padding:3px 9px;border-radius:999px;
             letter-spacing:.02em}
  .mrow.cameron .win{color:var(--a);background:var(--a-soft)}
  .mrow.paget .win{color:var(--b);background:var(--b-soft)}
  .mrow.draw .win{color:var(--muted);background:var(--faint)}
  .hist-empty{padding:16px 15px;color:var(--muted);font-size:13px;text-align:center}

   .settings-wrap{position:relative;flex:0 0 auto}
   .settings-trigger{display:grid;place-items:center;width:36px;height:36px;padding:0;
          border:1px solid var(--card-brd);border-radius:12px;background:var(--card);
          color:var(--muted);box-shadow:0 2px 8px rgba(18,38,74,.04)}
   .settings-trigger svg{display:block;width:18px;height:18px;stroke:currentColor;stroke-width:1.8;
          stroke-linecap:round;stroke-linejoin:round;fill:none}
   .settings-trigger:hover{color:var(--a);border-color:var(--ring)}
   .settings-popout{position:absolute;top:calc(100% + 9px);right:0;z-index:30;
          width:min(252px,calc(100vw - 32px));padding:11px;
          border:1px solid var(--card-brd);border-radius:16px;background:var(--card);
          box-shadow:0 18px 42px -18px rgba(0,0,0,.38);
          -webkit-backdrop-filter:blur(24px) saturate(140%);backdrop-filter:blur(24px) saturate(140%);
          animation:rise .16s ease-out}
   .settings-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
          padding:3px 4px 10px}
   .settings-head b{font-size:13px;font-weight:800}
   .settings-version{font-size:10px;font-weight:700;letter-spacing:.04em;color:var(--muted);
          font-variant-numeric:tabular-nums}
   .settings-row{display:flex;align-items:center;justify-content:space-between;gap:12px;
          padding:7px 4px 9px;color:var(--ink);font-size:12px;font-weight:650}
   .themebtn{display:grid;place-items:center;width:36px;height:36px;flex:0 0 auto;padding:0;
          border:1px solid var(--card-brd);border-radius:11px;background:var(--faint);color:var(--muted)}
   .themebtn:hover{color:var(--a);border-color:var(--ring)}
   .themebtn svg{display:block;width:18px;height:18px;stroke:currentColor;stroke-width:1.8;
          stroke-linecap:round;stroke-linejoin:round;fill:none}
   .settings-divider{height:1px;margin:2px 0 6px;background:var(--card-brd)}
   .settings-action{display:flex;align-items:center;gap:10px;width:100%;min-height:40px;
          padding:0 8px;border:0;border-radius:10px;background:transparent;color:var(--ink);
          text-align:left;font-size:12px;font-weight:650}
   .settings-action:hover{background:var(--faint);color:var(--a)}
   .settings-action svg{width:17px;height:17px;flex:0 0 auto;stroke:currentColor;stroke-width:1.8;
          stroke-linecap:round;stroke-linejoin:round;fill:none}
   .settings-action.reset{color:var(--muted)}
   .settings-action.reset:hover{color:var(--a)}

  [hidden]{display:none !important}
  .scrim{
    position:fixed;inset:0;z-index:100;display:flex;align-items:center;justify-content:center;
    padding:24px;background:rgba(8,10,14,.5);
    -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
    animation:fade .16s ease-out;
  }
  @keyframes fade{from{opacity:0}to{opacity:1}}
  .modal{
    width:100%;max-width:340px;background:var(--card);border:1px solid var(--card-brd);
    border-radius:20px;box-shadow:0 24px 60px -16px rgba(0,0,0,.5);
    padding:22px 20px 18px;text-align:center;
    -webkit-backdrop-filter:blur(24px) saturate(140%);backdrop-filter:blur(24px) saturate(140%);
    animation:rise .2s cubic-bezier(.2,1.3,.5,1);
  }
  @keyframes rise{from{opacity:0;transform:translateY(10px) scale(.96)}to{opacity:1;transform:none}}
  .m-title{font-size:17.5px;font-weight:650;margin:0 0 6px}
  .m-sub{font-size:13.5px;color:var(--muted);margin:0 0 20px;line-height:1.4}
  .sheet{max-width:380px;text-align:left;max-height:84vh;overflow-y:auto;
         -webkit-overflow-scrolling:touch;overscroll-behavior:contain}
  .ver{margin:-2px 0 10px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);font-weight:650}
  .sheet h4{margin:14px 0 5px;font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;
            color:var(--muted);font-weight:700}
  .sheet h4:first-of-type{margin-top:6px}
  ul.info{margin:0 0 2px;padding:0 0 0 18px;font-size:13px;line-height:1.45;color:var(--ink)}
  ul.info li{margin:0 0 7px}
  ul.info li::marker{color:var(--muted)}
  ul.info b{font-weight:700}
  .m-row{display:flex;gap:10px}
  .m-row button{flex:1;padding:15px 0;font-size:15.5px;border-radius:13px}
  .m-no{color:var(--ink);background:var(--faint);border-color:var(--card-brd)}
  .m-no:hover{filter:brightness(.97)}
  .m-yes{color:#fff;background:var(--b);border:none;box-shadow:0 6px 16px -8px var(--b)}
  .m-yes:hover{filter:brightness(1.06)}

  @media (prefers-reduced-motion: reduce){
    *{animation-duration:.001ms !important;transition-duration:.001ms !important}
  }

  /* ---- Tally 2.0: the scoreboard mark sets the visual language ---------- */
  :root{
    --bg:#edf3ff;--card:#fff;--card-brd:#dbe5f6;--ink:#111a2d;--muted:#64718b;
    --faint:rgba(25,45,78,.055);--shadow:0 16px 42px -28px rgba(19,42,85,.38);
    --a:#2563eb;--a-soft:rgba(37,99,235,.12);--b:#f97316;--b-soft:rgba(249,115,22,.13);
    --ring:rgba(37,99,235,.3);--radius:24px
  }
  :root[data-theme="dark"]{
    color-scheme:dark;--bg:#090f1d;--card:#111b2d;--card-brd:rgba(164,190,238,.13);
    --ink:#f3f6fc;--muted:#96a5bf;--faint:rgba(255,255,255,.055);
    --shadow:0 20px 56px -30px rgba(0,0,0,.8);--a:#6d9cff;--a-soft:rgba(91,140,255,.17);
    --b:#ff9b55;--b-soft:rgba(255,140,58,.17);--ring:rgba(109,156,255,.4)
  }
  :root[data-theme="light"]{color-scheme:light}
  @media(prefers-color-scheme:dark){
    :root:not([data-theme="light"]){
      color-scheme:dark;--bg:#090f1d;--card:#111b2d;--card-brd:rgba(164,190,238,.13);
      --ink:#f3f6fc;--muted:#96a5bf;--faint:rgba(255,255,255,.055);
      --shadow:0 20px 56px -30px rgba(0,0,0,.8);--a:#6d9cff;--a-soft:rgba(91,140,255,.17);
      --b:#ff9b55;--b-soft:rgba(255,140,58,.17);--ring:rgba(109,156,255,.4)
    }
  }
  body{background:var(--bg);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       transition:background .25s ease,color .25s ease}
  .wrap{max-width:980px;padding:0 clamp(16px,4vw,34px)}
  header{padding:8px 2px 24px;gap:16px}
  .h-left{gap:12px}
  .logo{width:44px;height:44px;border-radius:13px;object-fit:cover;box-shadow:0 7px 20px -8px var(--a)}
  .titles{gap:4px}
  .brand{font-size:16px;letter-spacing:.11em;color:var(--ink);font-weight:800}
  .tagline{margin:0;color:var(--muted);font-size:11px;font-weight:550;letter-spacing:.015em}
  .period{font-size:11.5px;letter-spacing:.055em;color:var(--muted);font-weight:650}
  .h-right{gap:8px}
  .sync{padding:7px 10px;border:1px solid var(--card-brd);border-radius:999px;
        background:var(--card);font-size:11px;font-weight:650;min-height:32px}
   .settings-trigger{width:36px;height:36px}
  .update-btn{display:flex;align-items:center;gap:7px;min-height:36px;padding:0 11px;
              border:1px solid var(--card-brd);border-radius:12px;background:var(--card);
              color:var(--muted);font-size:11px;font-weight:700;white-space:nowrap}
  .update-glyph{font-size:15px;line-height:1;font-weight:850}
  .update-btn[data-ready="1"]{border-color:color-mix(in srgb,var(--a) 38%,var(--card-brd));
                               background:var(--a-soft);color:var(--a)}
  .overview{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin:2px 0 20px}
  .overview-copy{min-width:0}
  .eyebrow{margin:0 0 5px;color:var(--a);font-size:9px;font-weight:850;letter-spacing:.17em}
  .overview h1{margin:0;font-size:clamp(24px,5vw,34px);line-height:1.08;letter-spacing:-.045em}
  .overview .lead{margin:7px 0 0;min-height:1.25em;text-align:left;font-size:12px;font-weight:650}
  .life{flex:0 0 auto;align-items:center;flex-direction:row;justify-content:space-between;gap:18px;
        margin:0;padding:12px 15px;border:1px solid var(--card-brd);
        border-radius:16px;background:var(--card);box-shadow:var(--shadow)}
  .life .cap{font-size:10px;letter-spacing:.15em;color:var(--muted)}
  .life .vals{gap:9px}
  .life b{font-size:clamp(22px,4vw,30px);font-weight:800}
  .section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;
                margin:0 2px 10px;color:var(--ink);font-size:13px;font-weight:800}
  .section-head span+span{color:var(--muted);font-size:10.5px;font-weight:600}
  .grid{gap:14px}
  @media(min-width:640px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
  .card{padding:clamp(16px,2.6vw,24px);border-radius:22px;border:1px solid var(--card-brd);
        background:var(--card);box-shadow:var(--shadow);text-align:left;transition:transform .2s ease}
  .card::before{content:"";position:absolute;inset:0 0 auto;height:4px;background:var(--accent)}
  .card[data-lead="1"]{transform:translateY(-2px)}
  .card[data-who="cameron"]{--accent:var(--a);--accent-soft:var(--a-soft)}
  .card[data-who="paget"]{--accent:var(--b);--accent-soft:var(--b-soft)}
  .name{justify-content:flex-start;margin:0;color:var(--ink);font-size:14px;font-weight:750}
  .name::before{content:attr(data-initial);display:grid;place-items:center;width:28px;height:28px;
                border-radius:9px;background:var(--accent-soft);color:var(--accent);font-size:12px;
                font-weight:850;letter-spacing:0}
   .score{margin:14px 0 3px;font-size:clamp(70px,13vw,108px);font-weight:780;
          letter-spacing:.02em;text-align:center}
  .pts{margin:0 0 18px;font-size:10px;letter-spacing:.16em;color:var(--muted)}
  .row{gap:10px}
  .row button{min-height:54px;border-radius:15px}
  .sub{flex:0 0 82px;font-size:22px;background:var(--faint);border-color:var(--card-brd);color:var(--muted)}
  .add{font-size:18px;font-weight:800;background:var(--accent);box-shadow:0 8px 20px -10px var(--accent)}
  .nudge{padding:9px 14px;border-radius:12px;background:var(--a-soft);color:var(--a);
         font-size:12px;font-weight:700}
  .nudge:empty{display:none}
  .hist{margin-top:22px;border:1px solid var(--card-brd);border-radius:18px;
        background:var(--card);box-shadow:var(--shadow)}
  .hist-h{padding:15px 18px 12px}
  .hist-h b{color:var(--ink);font-size:10px;letter-spacing:.13em}
  .mrow{padding:13px 18px}
  .mrow.live{background:var(--a-soft)}
  .hist-empty{display:flex;align-items:center;gap:12px;padding:16px 18px;text-align:left}
  .empty-mark{display:grid;place-items:center;width:34px;height:34px;flex:0 0 auto;
              border-radius:11px;background:var(--a-soft);color:var(--a);font-size:18px;font-weight:800}
  .empty-copy b{display:block;color:var(--ink);font-size:12px}
  .empty-copy span{display:block;margin-top:2px;color:var(--muted);font-size:11px}
   .update-btn:hover{color:var(--a);border-color:var(--ring)}
  .modal{border:1px solid var(--card-brd);border-radius:22px;background:var(--card)}
  .sheet{max-width:440px}
  .m-title{font-size:19px;font-weight:800;letter-spacing:-.02em}
  .ver{font-variant-numeric:tabular-nums}
  .m-no{border:1px solid var(--card-brd)}
  .m-yes{background:var(--a);box-shadow:0 8px 18px -10px var(--a)}
  .update-sheet{text-align:left}
  .update-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
  .update-kicker{margin:0 0 3px;color:var(--a);font-size:9px;font-weight:850;letter-spacing:.17em}
  .update-head .m-title{margin:0}
  .update-close{width:34px;height:34px;flex:0 0 auto;padding:0;border:1px solid var(--card-brd);
                border-radius:11px;background:var(--faint);color:var(--muted);font-size:20px}
  .update-message{margin:0;color:var(--muted);font-size:12.5px;line-height:1.45}
  .update-sheet .update-state{margin:0 0 15px;padding:12px 14px;border-radius:13px;
        background:var(--a-soft);color:var(--a);font-size:13px;font-weight:700}
  .update-sheet .update-versions{display:flex;justify-content:space-between;gap:12px;margin:0 0 18px;
        color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
  .update-sheet .m-row{margin-top:12px}
  .update-sheet .update-check{background:var(--faint);color:var(--ink);border:1px solid var(--card-brd)}
  .update-sheet .update-install{background:var(--a);color:#fff}
  .update-sheet .update-install:disabled{opacity:.5}
  @media(max-width:520px){
    .wrap{padding:0 15px}
    header{flex-wrap:wrap;padding-bottom:17px}
    .h-right{gap:6px}
    .update-btn{width:36px;padding:0;justify-content:center}
    .update-btn .update-label{display:none}
    .overview{align-items:flex-start;flex-direction:column;gap:12px}
    .life{width:100%;padding:11px 13px}
    .sub{flex-basis:66px}
  }
</style>
</head>
<body>
<div class="tint a" id="tintA"></div>
<div class="tint b" id="tintB"></div>

<div class="wrap">
  <header>
   <div class="h-left">
     <img class="logo" src="/icon-192.png?v=__APP_VERSION__" alt="Tally logo">
     <div class="titles">
       <p class="brand">Tally</p>
       <p class="tagline">A little friendly competition</p>
       <p class="period" id="period"></p>
     </div>
    </div>
     <div class="h-right">
      <span class="sync" id="sync" title="connection"><span class="dot"></span><span id="syncText"></span></span>
       <button class="update-btn" id="updates" type="button" aria-label="Tally updates" data-ready="0">
        <span class="update-glyph" aria-hidden="true">↓</span><span class="update-label" id="updateLabel">Updates</span></button>
      <div class="settings-wrap" id="settingsWrap">
       <button class="settings-trigger" id="settingsToggle" type="button" aria-label="Settings"
               aria-expanded="false" aria-controls="settingsMenu" title="Settings">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="17" r="2"/></svg>
       </button>
       <div class="settings-popout" id="settingsMenu" hidden>
        <div class="settings-head"><b>Settings</b><span class="settings-version" id="settingsVersion"></span></div>
        <div class="settings-row"><span>Appearance</span>
         <button class="themebtn" id="themeToggle" type="button" role="switch" aria-checked="false" aria-label="Dark mode">
          <svg id="themeSun" viewBox="0 0 24 24" aria-hidden="true" hidden>
           <circle cx="12" cy="12" r="3.5"/><path d="M12 2.5v2M12 19.5v2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M2.5 12h2m15 0h2M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/>
          </svg>
          <svg id="themeMoon" viewBox="0 0 24 24" aria-hidden="true" hidden>
           <path d="M20.2 15.4A8.6 8.6 0 0 1 8.6 3.8 8.7 8.7 0 1 0 20.2 15.4Z"/>
          </svg>
         </button>
        </div>
        <div class="settings-divider"></div>
        <button class="settings-action" id="info" type="button">
         <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg>
         <span>How Tally works</span>
        </button>
        <button class="settings-action reset" id="reset" type="button">
         <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 2.6-6.4L3 8"/><path d="M3 3v5h5"/></svg>
         <span>Reset this month</span>
        </button>
       </div>
      </div>
    </div>
  </header>

   <div class="overview">
     <div class="overview-copy">
       <p class="eyebrow">THIS MONTH</p>
       <h1>The scoreboard</h1>
       <p class="lead" id="lead"></p>
     </div>
     <div class="life">
       <span class="cap">LIFETIME POINTS</span>
       <div class="vals"><b class="lc" id="lifeC">0</b><span class="sep">\u00b7</span><b class="lp" id="lifeP">0</b></div>
     </div>
   </div>

   <div class="section-head"><span>Monthly score</span><span>Tap to count a point</span></div>
   <div class="grid" id="grid"></div>

  <p class="nudge" id="nudge" role="status"></p>

  <section class="hist" id="hist" hidden>
    <div class="hist-h"><b>Monthly wins</b><span class="tally-sum" id="sum"></span></div>
    <div id="months"></div>
  </section>

</div>

<div class="scrim" id="infoScrim" hidden>
  <div class="modal sheet" role="dialog" aria-modal="true" aria-labelledby="i-title">
    <p class="m-title" id="i-title">How Tally works</p>
     <h4>Scoring</h4>
    <ul class="info">
      <li><b>+1</b> gives a point, <b>&minus;1</b> takes one back. Neither side can go below zero.</li>
      <li>The two big numbers are <b>this month only</b>. They reset to 0 by themselves at midnight on the 1st, Melbourne time.</li>
      <li>The pair at the very top are <b>lifetime totals</b>. Those never reset.</li>
      <li>The line under them always refers to <b>this month</b>, not lifetime.</li>
    </ul>
    <h4>What the app is telling you</h4>
    <ul class="info">
      <li>The <b>blue and orange accents</b> keep the two players easy to distinguish.</li>
      <li>The leader gets a subtle highlight, while the score remains the focus.</li>
      <li>Fall <b>5 behind</b> and a line of encouragement shows up for whoever is trailing.</li>
      <li>Fall <b>10 behind</b> and that player&rsquo;s <b>name pulses red</b> until the gap closes.</li>
      <li>Every <b>10 points</b> in a month sets off a full-screen celebration on both phones.</li>
    </ul>
    <h4>Months</h4>
    <ul class="info">
      <li>When a month ends its result drops into <b>Monthly wins</b> with the winner marked, and the running count of months won updates.</li>
      <li><b>Reset</b> clears this month on both sides. Lifetime totals and finished months are kept.</li>
    </ul>
     <h4>Syncing</h4>
     <ul class="info">
       <li>Both phones refresh each other about every <b>2.5 seconds</b> \u2014 no need to reload.</li>
       <li>Everything lives on the NAS, so the score is the same on every device.</li>
     </ul>
     <h4>Updates</h4>
     <ul class="info">
       <li>Use the update control in the header to check GitHub and install a newer version.</li>
       <li>The host worker backs up the app files and redeploys Tally; your scores and history are untouched.</li>
     </ul>
    <div class="m-row"><button class="m-no" id="i-close" type="button">Got it</button></div>
  </div>
</div>

<div class="scrim" id="updateScrim" hidden>
  <div class="modal update-sheet" role="dialog" aria-modal="true" aria-labelledby="updateTitle">
    <div class="update-head"><div><p class="update-kicker">GITHUB</p><p class="m-title" id="updateTitle">Update Tally</p></div>
      <button class="update-close" id="updateClose" type="button" aria-label="Close">&times;</button></div>
    <p class="update-state" id="updateState" role="status">Checking GitHub</p>
    <div class="update-versions"><span>Installed <b id="updateCurrent"></b></span>
      <span>Latest <b id="updateLatest"></b></span></div>
    <p class="update-message" id="updateMessage">Updates are checked against Boldestfutura/Tally.</p>
    <div class="m-row">
      <button class="update-check" id="updateCheck" type="button">Check for updates</button>
      <button class="update-install" id="updateInstall" type="button" hidden>Install update</button>
    </div>
  </div>
</div>

<div class="scrim" id="scrim" hidden>
  <div class="modal" role="dialog" aria-modal="true" aria-labelledby="m-title" aria-describedby="m-sub">
    <p class="m-title" id="m-title">Reset this month?</p>
    <p class="m-sub" id="m-sub">This month goes back to nil-all. Lifetime totals and past months are kept.</p>
    <div class="m-row">
      <button class="m-no" id="m-no" type="button">No</button>
      <button class="m-yes" id="m-yes" type="button">Yes, reset</button>
    </div>
  </div>
</div>

<script>
const APP_VERSION='__APP_VERSION__';
const PLAYERS=[["cameron","Cameron"],["paget","Paget"]];
const NAME={cameron:'Cameron',paget:'Paget'};

// shown to whoever is 5 or more behind. {name} becomes the trailing player.
const RALLY=[
 "Still anyone's game, {name}.",
 "{name}, comebacks start with one point.",
 "Nobody remembers the halfway score.",
 "Five is nothing. Go and get one back.",
 "{name}, dig in.",
 "The gap looks bigger than it is.",
 "One good run and this flips.",
 "{name}, you've been behind before.",
 "The pressure sits with the leader now.",
 "Chip away, {name}.",
 "Momentum turns fast.",
 "{name}, start the fightback.",
 "Leads are rented, never owned.",
 "Plenty left in this, {name}.",
 "Quietly close the gap.",
 "{name}, time to make it interesting.",
 "Bigger leads than this have gone.",
 "Deep breath, {name}. One at a time.",
 "The scoreboard isn't the final word.",
 "{name}, make them nervous.",
 "Underdogs are more fun to watch anyway.",
 "Keep going, {name} \\u2014 it only takes a streak.",
 "Not out of it. Not even close.",
 "{name}, prove the gap wrong.",
 "Every comeback looks impossible right up until it isn't.",
 "Stay in it, {name}.",
 "One point, then another.",
 "They can still feel you back there.",
 "{name}, this is where it gets good.",
 "Long way to go yet."
];
let scores={cameron:0,paget:0};   // this month
let life={cameron:0,paget:0};     // all time
let period='';
let months=[];
let savedAt=0;                    // when the numbers below last came from the server
let shown={cameron:null,paget:null};
let inflight=0;

const grid=document.getElementById('grid');
for(const [who,label] of PLAYERS){
  const c=document.createElement('section');
  c.className='card'; c.dataset.who=who; c.id='card-'+who;
  c.innerHTML=
    '<p class="name" data-initial="'+label[0]+'">'+label+'</p>'+
    '<div class="score" id="s-'+who+'"><span class="n">0</span></div>'+
    '<p class="pts">this month</p>'+
    '<div class="row">'+
      '<button class="sub" data-who="'+who+'" data-delta="-1" aria-label="Remove a point from '+label+'">\\u22121</button>'+
      '<button class="add" data-who="'+who+'" data-delta="1" aria-label="Add a point to '+label+'">+1</button>'+
    '</div>';
  grid.appendChild(c);
}

function buzz(ms){ try{ if(navigator.vibrate) navigator.vibrate(ms); }catch(e){} }

const calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function accentOf(who){
  const c=document.getElementById('card-'+who);
  return getComputedStyle(c).getPropertyValue('--accent').trim() || '#2f6df6';
}

function parseColor(s){
  s=(s||'').trim();
  let m=s.match(/^#([0-9a-f]{3,8})$/i);
  if(m){
    let h=m[1];
    if(h.length===3) h=h.split('').map(c=>c+c).join('');
    return [parseInt(h.slice(0,2),16),parseInt(h.slice(2,4),16),parseInt(h.slice(4,6),16)];
  }
  m=s.match(/rgba?\\(([^)]+)\\)/i);
  if(m){ const p=m[1].split(',').map(x=>parseFloat(x)); return [p[0]||0,p[1]||0,p[2]||0]; }
  return [47,109,246];
}
function mix(rgb,target,amt){
  return 'rgb('+rgb.map((c,i)=>Math.round(c+(target[i]-c)*amt)).join(',')+')';
}
// three shades of the one player colour, so a burst is unmistakably theirs
function paletteFor(who){
  const base=parseColor(accentOf(who));
  return ['rgb('+base.map(Math.round).join(',')+')',
          mix(base,[255,255,255],.45),
          mix(base,[0,0,0],.24)];
}

// plus -> confetti fans up out of the button; minus -> a small grey puff drops out of it
function burst(btn, who, kind){
  if(calm || !btn.animate) return;
  const r=btn.getBoundingClientRect();
  const cx=r.left+r.width/2, cy=r.top+r.height/2;
  const shades = kind==='add' ? paletteFor(who) : null;
  const n = kind==='add' ? 24 : 7;
  // spread relative to the card so the burst fills that player's box
  const cardRect=document.getElementById('card-'+who).getBoundingClientRect();
  const reach=Math.min(cardRect.width*0.46, 210);
  const frag=document.createDocumentFragment();
  const nodes=[];
  for(let i=0;i<n;i++){
    const p=document.createElement('span');
    p.className='fx '+(kind==='add'?'conf':'puff');
    p.style.left=cx+'px'; p.style.top=cy+'px';
    p.style.background = kind==='add' ? shades[i%3] : 'var(--muted)';
    if(kind!=='add') p.style.opacity='.75';
    frag.appendChild(p); nodes.push(p);
  }
  document.body.appendChild(frag);

  nodes.forEach(p=>{
    let deg, dist, drop, dur;
    if(kind==='add'){
      deg  = -90 + (Math.random()*168 - 84);        // wide fan, nearly flat out to the sides
      dist = reach*0.45 + Math.random()*reach*0.62;  // scales with the card width
      drop = 58 + Math.random()*58;                 // then gravity pulls it down
      dur  = 2600 + Math.random()*1900;             // same pace as the milestone shower
    }else{
      deg  = 90 + (Math.random()*70 - 35);          // sink downward
      dist = 26 + Math.random()*30;
      drop = 26;                                    // a little more travel so the slower fall still reads
      dur  = 2600 + Math.random()*1900;             // same pace as the confetti
    }
    const rad=deg*Math.PI/180;
    const dx=Math.cos(rad)*dist, dy=Math.sin(rad)*dist;
    const rot=Math.random()*720-360;
    const base='translate(-50%,-50%) ';
    p.animate([
      {transform:base+'translate(0,0) rotate(0deg)', opacity:1},
      {transform:base+'translate('+(dx*0.72)+'px,'+dy+'px) rotate('+(rot*0.6)+'deg)', opacity:1, offset:.55},
      {transform:base+'translate('+dx+'px,'+(dy+drop)+'px) rotate('+rot+'deg)', opacity:0}
    ],{duration:dur, easing:'cubic-bezier(.22,.6,.36,1)', fill:'forwards'});
    setTimeout(()=>p.remove(), dur+90);
  });
}

function floatPop(who,delta){
  const card=document.getElementById('card-'+who);
  const el=document.createElement('span');
  el.className='pop'; el.textContent=(delta>0?'+':'\\u2212')+'1';
  card.appendChild(el);
  setTimeout(()=>el.remove(),780);
}

// every multiple of 10 gets a full-screen shower in that player's colours
function celebrate(who, score){
  const shades=paletteFor(who);
  const label = who==='cameron' ? 'Cameron' : 'Paget';
  buzz([18,60,18,60,28]);

  if(!calm && document.body.animate!==undefined){
    const H=window.innerHeight;
    const frag=document.createDocumentFragment();
    const nodes=[];
    for(let i=0;i<190;i++){
      const p=document.createElement('span');
      p.className='fx rain';
      p.style.left=(Math.random()*104-2)+'vw';     // overshoot the edges so the sides fill too
      p.style.top=(-8-Math.random()*22)+'vh';      // staggered start heights = a deeper shower
      p.style.background=shades[i%3];
      const w=6+Math.random()*6;
      p.style.width=w.toFixed(1)+'px';
      p.style.height=(w*1.5+Math.random()*5).toFixed(1)+'px';
      if(i%6===0){ p.style.height=p.style.width; p.style.borderRadius='50%'; }
      frag.appendChild(p); nodes.push(p);
    }
    document.body.appendChild(frag);
    nodes.forEach(p=>{
      const drift=Math.random()*240-120;
      const rot=Math.random()*1200-600;
      const dur=2600+Math.random()*1900;
      const delay=Math.random()*1200;
      p.animate([
        {transform:'translate(0,0) rotate(0deg)', opacity:0},
        {transform:'translate('+(drift*.45)+'px,'+(H*.42)+'px) rotate('+(rot*.45)+'deg)', opacity:1, offset:.3},
        {transform:'translate('+drift+'px,'+(H*1.12)+'px) rotate('+rot+'deg)', opacity:1, offset:.9},
        {transform:'translate('+drift+'px,'+(H*1.2)+'px) rotate('+(rot*1.05)+'deg)', opacity:0}
      ],{duration:dur, delay:delay, easing:'cubic-bezier(.25,.5,.5,1)', fill:'forwards'});
      setTimeout(()=>p.remove(), dur+delay+150);
    });
  }

  const wrap=document.createElement('div');
  wrap.className='cheer';
  wrap.setAttribute('role','status');
  const b=document.createElement('b');
  b.style.color=shades[0];
  b.textContent=MILESTONE[mix(score)%MILESTONE.length]
                  .replace('{name}',label).replace('{n}',score);
  wrap.appendChild(b);
  document.body.appendChild(wrap);
  setTimeout(()=>wrap.remove(), 2550);
}

const MILESTONE=[
 "{name} hits {n}!",
 "Another ten for {name}!",
 "{n} and counting, {name}!",
 "{name} racks up {n}!",
 "Ten more \u2014 {name} on {n}!",
 "That's {n} for {name}!",
 "{name} banks another ten!",
 "{n} up! Take a bow, {name}.",
 "{name} rolls on to {n}!",
 "Another ten in the bag, {name}.",
 "{name} makes it {n}!",
 "{name} is on {n} and climbing."
];

const MONTHS=['January','February','March','April','May','June',
              'July','August','September','October','November','December'];

function monthLabel(period){           // "2026-09" -> "September 2026"
  const m=/^(\\d{4})-(\\d{2})$/.exec(period||'');
  if(!m) return period||'';
  return MONTHS[parseInt(m[2],10)-1]+' '+m[1];
}

function renderMonths(){
  document.getElementById('period').textContent=monthLabel(period);

  const box=document.getElementById('months');
  const panel=document.getElementById('hist');
  const live = scores.cameron || scores.paget;
  if(!months.length && !live){
    panel.hidden=false;
    document.getElementById('sum').textContent='';
    box.innerHTML='<div class="hist-empty"><span class="empty-mark">01</span>'+
      '<span class="empty-copy"><b>No completed months yet</b>'+
      '<span>Your first monthly result will appear here.</span></span></div>';
    return;
  }
  panel.hidden=false;

  const frag=document.createDocumentFragment();

  // the month in progress sits on top, with no winner called yet
  if(live || months.length){
    const w = scores.cameron>scores.paget ? 'cameron'
            : (scores.paget>scores.cameron ? 'paget' : 'draw');
    const row=document.createElement('div');
    row.className='mrow live '+w;
    row.innerHTML=
      '<span class="mon">'+monthLabel(period)+' <em>so far</em></span>'+
      '<span class="sc">'+scores.cameron+'\u2013'+scores.paget+'</span>'+
      '<span class="win">'+(w==='draw'?'Level':NAME[w]+' ahead')+'</span>';
    frag.appendChild(row);
  }

  let wc=0, wp=0, dr=0;
  const sorted=months.slice().sort((a,b)=> a.period<b.period ? 1 : (a.period>b.period ? -1 : 0));
  for(let i=0;i<sorted.length;i++){
    const m=sorted[i];
    const win = m.cameron>m.paget ? 'cameron' : (m.paget>m.cameron ? 'paget' : 'draw');
    if(win==='cameron') wc++; else if(win==='paget') wp++; else dr++;
    const row=document.createElement('div');
    row.className='mrow '+win;
    row.innerHTML=
      '<span class="mon">'+monthLabel(m.period)+'</span>'+
      '<span class="sc">'+m.cameron+'\u2013'+m.paget+'</span>'+
      '<span class="win">'+(win==='draw'?'Drawn':NAME[win]+' wins!')+'</span>';
    frag.appendChild(row);
  }
  box.replaceChildren(frag);

  document.getElementById('sum').innerHTML = months.length
    ? 'Months won \u00b7 Cameron <i style="color:var(--a)">'+wc+'</i> \u00b7 Paget <i style="color:var(--b)">'+wp+'</i>'+(dr?' \u00b7 drawn '+dr:'')
    : '';
}

// integer hash — spreads neighbouring scores across the whole message pool
function mix(n){
  n=(n^61)^(n>>>16); n=n+(n<<3); n=n^(n>>>4);
  n=Math.imul(n,0x27d4eb2d); n=n^(n>>>15);
  return n>>>0;
}

// odometer-style swap: the new number eases in while the old one slides out behind it
const rolling={cameron:{anims:[],ghost:null}, paget:{anims:[],ghost:null}};

function roll(box, who, value, up){
  const cur=box.querySelector('.n');
  if(!cur){ box.textContent=value; return; }
  if(calm || !cur.animate){ cur.textContent=value; return; }

  // tear down anything still in flight: two rolls animating the same element at once
  // is what made the number jitter when points came in quickly
  const st=rolling[who];
  st.anims.forEach(a=>{ try{ a.cancel(); }catch(e){} });
  st.anims=[];
  if(st.ghost){ st.ghost.remove(); st.ghost=null; }

  const h=box.offsetHeight || 80;
  const travel=h*0.66;
  const dir = up ? 1 : -1;                 // counting up: the new value rises from below
  const ease='cubic-bezier(.22,.82,.26,1)';
  const dur=380;

  const ghost=cur.cloneNode(true);         // clone first, so it keeps the OLD number
  ghost.classList.add('ghost');
  box.appendChild(ghost);
  st.ghost=ghost;

  // put the incoming number in its start position before it is painted,
  // otherwise it flashes once at rest before the animation takes over
  cur.style.transform='translateY('+(dir*travel)+'px)';
  cur.style.opacity='0';
  cur.textContent=value;

  const a1=cur.animate([
    {transform:'translateY('+(dir*travel)+'px)', opacity:0},
    {transform:'translateY(0)', opacity:1}
  ],{duration:dur, easing:ease, fill:'both'});

  const a2=ghost.animate([
    {transform:'translateY(0)', opacity:1},
    {transform:'translateY('+(-dir*travel)+'px)', opacity:0}
  ],{duration:dur, easing:ease, fill:'both'});

  st.anims=[a1,a2];
  a2.onfinish=()=>{
    if(st.ghost===ghost){ ghost.remove(); st.ghost=null; }
    try{ a1.cancel(); }catch(e){}
    cur.style.transform=''; cur.style.opacity='';
    st.anims=[];
  };
}

function render(){
  const total=scores.cameron+scores.paget;
  for(const [who] of PLAYERS){
    const el=document.getElementById('s-'+who);
    if(shown[who]!==scores[who]){
      const up = shown[who]===null ? null : scores[who] > shown[who];
      if(up===null){ const n=el.querySelector('.n'); if(n) n.textContent=scores[who]; }
      else roll(el, who, scores[who], up);
      shown[who]=scores[who];
      // only on the way up, so correcting 11 back to 10 doesn't throw a party
      if(up===true && scores[who]>0 && scores[who]%10===0) celebrate(who, scores[who]);
    }
    const other = who==='cameron' ? scores.paget : scores.cameron;
    const lead = total>0 && scores[who]>other;
    const card = document.getElementById('card-'+who);
    card.dataset.lead = lead ? '1':'0';
    card.dataset.trail = (other - scores[who]) >= 10 ? '1':'0';
  }

  // weight the two background washes toward whoever is ahead this month
  const share = total>0 ? scores.cameron/total : 0.5;
  document.getElementById('tintA').style.opacity=(0.3 + 1.4*share).toFixed(3);
  document.getElementById('tintB').style.opacity=(0.3 + 1.4*(1-share)).toFixed(3);

  const d=scores.cameron-scores.paget;
  const lead=document.getElementById('lead');
  if(total===0) lead.textContent='Nil-all this month';
  else if(d===0) lead.textContent='All square this month at '+scores.cameron;
  else lead.textContent=(d>0?'Cameron':'Paget')+' leads by '+Math.abs(d)+' this month';

  document.querySelectorAll('.sub').forEach(b=>{ b.disabled = scores[b.dataset.who]<=0; });
  document.getElementById('lifeC').textContent=life.cameron;
  document.getElementById('lifeP').textContent=life.paget;
  renderMonths();

  // derived from the score itself, so both phones land on the same message
  const nudge=document.getElementById('nudge');
  const gap=Math.abs(d);
  if(gap>=5){
    const behind = d>0 ? 'paget' : 'cameron';
    const idx = mix(scores.cameron*1013 + scores.paget*7919 + gap) % RALLY.length;
    const text = RALLY[idx].replace('{name}', NAME[behind]);
    if(nudge.dataset.text!==text){
      nudge.dataset.text=text;
      nudge.innerHTML='';
      const s=document.createElement('span');
      s.textContent=text;
      s.style.color = behind==='cameron' ? 'var(--a)' : 'var(--b)';
      nudge.appendChild(s);
    }
  }else if(nudge.dataset.text){
    nudge.dataset.text='';
    nudge.innerHTML='';
  }
}

/* ---- taps that have not reached the server yet --------------------------
   A tap used to go straight out and be forgotten if it failed, so a point
   added with no signal went up on screen and then vanished at the next poll,
   silently. Every tap is now written down first and only crossed off when the
   server says it counted it. */
const QKEY='tally.queue.v1';
function queue(){
  try{ const q=JSON.parse(localStorage.getItem(QKEY)); return Array.isArray(q)?q:[]; }
  catch(e){ return []; }
}
function setQueue(q){
  try{ localStorage.setItem(QKEY, JSON.stringify(q.slice(-400))); }catch(e){}
}
function newId(){
  if(self.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'q'+Date.now().toString(36)+Math.random().toString(36).slice(2,10);
}
function pendingFor(who){
  return queue().reduce((a,e)=>a+(e.who===who?e.delta:0),0);
}
function pendingCount(){ return queue().length; }

/* What is waiting, said accurately. Counting the queue and calling it "points
   waiting" read a tap on the minus button as another point to add, which is
   the opposite of what it was. */
function pendingWord(){
  const q=queue();
  if(!q.length) return '';
  let up=0, down=0;
  q.forEach(e=>{ if(e.delta>0) up++; else down++; });
  if(!down) return up===1 ? '1 point waiting' : up+' points waiting';
  if(!up)   return down===1 ? '1 to take off' : down+' to take off';
  return (up+down)+' changes waiting';
}

function enqueue(who, delta){
  const q=queue();
  // A tap that undoes one still waiting cancels it rather than queueing its
  // opposite. Otherwise +1 then -1 reads as "2 points waiting" when the answer
  // is that nothing is owed at all. Neither ever reaches the server, which is
  // correct: together they were a no-op.
  for(let i=q.length-1;i>=0;i--){
    if(q[i].who===who && q[i].delta===-delta){
      q.splice(i,1); setQueue(q); return;
    }
  }
  q.push({id:newId(), who:who, delta:delta, t:Date.now()});
  setQueue(q);
}

/* The scoreboard itself, kept on the phone.

   Caching the app without caching the scores meant opening it cold on a plane
   showed zeros and the word "reconnecting" - the shell loaded, the fetch
   failed, and there was nothing to fall back on. What is stored here is what
   the SERVER last said, never what is on screen: the screen already has the
   unsent taps added on top, and storing that would count them twice on the
   next load. */
const SKEY='tally.state.v1';

function remember(data){
  try{
    localStorage.setItem(SKEY, JSON.stringify({
      cameron:data.cameron, paget:data.paget,
      month:data.month, period:data.period, months:data.months,
      t:Date.now()}));
  }catch(e){}
}

function recall(){
  try{
    const d=JSON.parse(localStorage.getItem(SKEY));
    return (d && typeof d==='object' && typeof d.cameron==='number') ? d : null;
  }catch(e){ return null; }
}

function ago(ms){
  const s=Math.max(0,Math.round((Date.now()-ms)/1000));
  if(s<90) return 'just now';
  const m=Math.round(s/60);
  if(m<90) return m+' min ago';
  const h=Math.round(m/60);
  if(h<36) return h+'h ago';
  return Math.round(h/24)+'d ago';
}

function adopt(data){
  remember(data);                 // the server's view, before anything local
  if(typeof data.t==='number') savedAt=data.t; else savedAt=Date.now();
  // the server's number plus whatever this phone is still holding, so the
  // screen keeps showing what you tapped rather than snapping back
  life={cameron:data.cameron + pendingFor('cameron'),
        paget:  data.paget   + pendingFor('paget')};
  if(data.month) scores={cameron:Math.max(0,data.month.cameron + pendingFor('cameron')),
                         paget:  Math.max(0,data.month.paget   + pendingFor('paget'))};
  if(typeof data.period==='string') period=data.period;
  if(Array.isArray(data.months)) months=data.months;
}

function setSync(ok){
  const s=document.getElementById('sync');
  const n=pendingCount();
  s.dataset.off = (ok && !n) ? '0':'1';
  // stay silent while everything is through; otherwise say what is owed and,
  // with no signal, how old the numbers on screen are - "reconnecting" on its
  // own tells you nothing about whether you can trust what you are looking at
  const t=document.getElementById('syncText');
  if(n) t.textContent = pendingWord();
  else if(ok) t.textContent = '';
  else t.textContent = savedAt ? 'offline \u00b7 ' + ago(savedAt) : 'reconnecting';
}

async function post(path,body){
  inflight++;
  try{
    const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
                             body:JSON.stringify(body||{})});
    if(!r.ok) throw new Error(r.status);
    adopt(await r.json()); render(); setSync(true);
  }catch(e){ setSync(false); }
  finally{ inflight--; }
}

let syncing=false;
async function sync(){
  if(syncing) return;
  const q=queue();
  if(!q.length) return poll();
  syncing=true;
  try{
    const r=await fetch('/api/sync',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({events:q})});
    if(!r.ok) throw new Error(r.status);
    const d=await r.json();
    const ok={};
    (d.accepted||[]).forEach(id=>ok[id]=1);
    setQueue(queue().filter(e=>!ok[e.id]));     // only cross off what it counted
    adopt(d); render(); setSync(true);
  }catch(e){ setSync(false); }
  finally{ syncing=false; }
}

async function poll(){
  if(inflight||syncing) return;
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    adopt(await r.json()); render(); setSync(true);
  }catch(e){ setSync(false); }
}

document.addEventListener('click',ev=>{
  const b=ev.target.closest('button[data-who]');
  if(!b||b.disabled) return;
  const who=b.dataset.who, delta=parseInt(b.dataset.delta,10);
  if(delta<0 && scores[who]<=0) return;      // nothing to take off this month
  scores[who]=Math.max(0,scores[who]+delta);
  life[who]=Math.max(0,life[who]+delta);
  burst(b, who, delta>0?'add':'sub');
  floatPop(who,delta);
  buzz(delta>0?14:8);
  enqueue(who, delta);
  render();
  setSync(true);
  sync();
});

// a phone that has just found signal again should not wait for the next tick
window.addEventListener('online', sync);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) sync(); });

if('serviceWorker' in navigator){
  window.addEventListener('load',()=>{
    navigator.serviceWorker.register('/sw.js').catch(()=>{});
  });
}

// Browsers clear storage for sites you have not opened in a while, which would
// take the cached app and the queue of unsent points with it. Asking does not
// always get you a yes, but not asking never does.
if(navigator.storage && navigator.storage.persist){
  navigator.storage.persisted().then(p=>{ if(!p) navigator.storage.persist(); })
                               .catch(()=>{});
}

// The app can check GitHub and ask the host worker to install a release.
const updateButton=document.getElementById('updates');
const updateScrim=document.getElementById('updateScrim');
const updateLabel=document.getElementById('updateLabel');
const updateState=document.getElementById('updateState');
const updateCurrent=document.getElementById('updateCurrent');
const updateLatest=document.getElementById('updateLatest');
const updateMessage=document.getElementById('updateMessage');
const updateCheckButton=document.getElementById('updateCheck');
const updateInstallButton=document.getElementById('updateInstall');
let updateBusy=false;

function paintUpdate(d){
  d=d||{};
  const latest=d.latest||APP_VERSION, available=!!d.available;
  const busy=['queued','installing','restarting'].includes(d.state);
  updateCurrent.textContent='v'+(d.current||APP_VERSION);
  updateLatest.textContent='v'+latest;
  updateButton.dataset.ready=available?'1':'0';
  updateLabel.textContent=available?'Update available':'Updates';
  updateState.textContent=available?'Version v'+latest+' is ready to install':
    busy?(d.message||'Installing update…'):
    d.state==='error'?(d.message||'Could not check GitHub'):
    d.message||'Tally is up to date';
  updateMessage.textContent=available?'Your score data stays on the NAS. Tally will restart briefly during the update.':
    busy?'The host worker will install the update and restart Tally.':
    'Checks Boldestfutura/Tally on GitHub. Your score history stays untouched.';
  updateInstallButton.hidden=!available;
  updateInstallButton.disabled=busy||updateBusy;
  updateCheckButton.disabled=busy||updateBusy;
}

async function refreshUpdateStatus(){
  try{
    const r=await fetch('/api/update/status',{cache:'no-store'});
    if(r.ok) paintUpdate(await r.json());
  }catch(e){}
}

async function checkForUpdate(){
  updateBusy=true; updateCheckButton.disabled=true; updateState.textContent='Checking GitHub…';
  try{
    const r=await fetch('/api/update/check',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    paintUpdate(await r.json());
    if(!r.ok) updateMessage.textContent='Could not check GitHub. Try again when the connection is back.';
  }catch(e){ updateState.textContent='Could not reach GitHub'; }
  finally{ updateBusy=false; updateCheckButton.disabled=false; }
}

async function installUpdate(){
  updateInstallButton.disabled=true; updateState.textContent='Queuing update…';
  try{
    const r=await fetch('/api/update/install',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d=await r.json();
    updateMessage.textContent=d.message||d.error||'Update request sent';
    if(r.ok){ updateState.textContent='Update queued'; updateButton.dataset.ready='0'; }
    else updateState.textContent=d.error||'Could not queue update';
  }catch(e){ updateState.textContent='Could not reach the NAS'; }
  setTimeout(refreshUpdateStatus,1200);
}

const themeButton=document.getElementById('themeToggle');
const themeSun=document.getElementById('themeSun');
const themeMoon=document.getElementById('themeMoon');
function setTheme(theme,save){
  document.documentElement.dataset.theme=theme;
  themeSun.toggleAttribute('hidden',theme!=='dark');
  themeMoon.toggleAttribute('hidden',theme==='dark');
  themeButton.setAttribute('aria-checked',theme==='dark'?'true':'false');
  themeButton.title=theme==='dark'?'Switch to light mode':'Switch to dark mode';
  const color=theme==='dark'?'#090f1d':'#edf3ff';
  document.getElementById('themeLight').content=color;
  document.getElementById('themeDark').content=color;
  if(save){ try{localStorage.setItem('tally.theme.v1',theme);}catch(e){} }
}
let savedTheme='';
try{ savedTheme=localStorage.getItem('tally.theme.v1')||''; }catch(e){}
setTheme(savedTheme==='light'||savedTheme==='dark'?savedTheme:
  (matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light'),false);
themeButton.addEventListener('click',()=>{
  const current=document.documentElement.dataset.theme||
    (matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
  setTheme(current==='dark'?'light':'dark',true);
});
const settingsWrap=document.getElementById('settingsWrap');
const settingsToggle=document.getElementById('settingsToggle');
const settingsMenu=document.getElementById('settingsMenu');
function closeSettings(returnFocus){
  settingsMenu.hidden=true;
  settingsToggle.setAttribute('aria-expanded','false');
  if(returnFocus) settingsToggle.focus();
}
settingsToggle.addEventListener('click',()=>{
  const opening=settingsMenu.hidden;
  settingsMenu.hidden=!opening;
  settingsToggle.setAttribute('aria-expanded',opening?'true':'false');
});
document.addEventListener('click',ev=>{
  if(!settingsMenu.hidden&&!settingsWrap.contains(ev.target)) closeSettings(false);
});
document.addEventListener('keydown',ev=>{
  if(!settingsMenu.hidden&&ev.key==='Escape'){ev.preventDefault();closeSettings(true);}
});
document.getElementById('settingsVersion').textContent='Version '+APP_VERSION;
updateButton.addEventListener('click',()=>{
  updateScrim.hidden=false; refreshUpdateStatus(); updateCheckButton.focus();
});
document.getElementById('updateClose').addEventListener('click',()=>{updateScrim.hidden=true;updateButton.focus();});
updateScrim.addEventListener('click',ev=>{if(ev.target===updateScrim){updateScrim.hidden=true;updateButton.focus();}});
document.addEventListener('keydown',ev=>{
  if(!updateScrim.hidden && ev.key==='Escape'){updateScrim.hidden=true;updateButton.focus();}
});
updateCheckButton.addEventListener('click',checkForUpdate);
updateInstallButton.addEventListener('click',installUpdate);
refreshUpdateStatus();
setInterval(refreshUpdateStatus,15000);

const resetBtn=document.getElementById('reset');
const scrim=document.getElementById('scrim');
const noBtn=document.getElementById('m-no');
const yesBtn=document.getElementById('m-yes');
let lastFocus=null;

function openAsk(){
  lastFocus=document.activeElement;
  scrim.hidden=false;
  noBtn.focus();
}
function closeAsk(){
  scrim.hidden=true;
  if(lastFocus && lastFocus.focus) lastFocus.focus();
}

resetBtn.addEventListener('click',()=>{closeSettings(false);settingsToggle.focus();openAsk();});

const infoScrim=document.getElementById('infoScrim');
const infoBtn=document.getElementById('info');
const infoClose=document.getElementById('i-close');
infoBtn.addEventListener('click',()=>{ closeSettings(false); infoScrim.hidden=false; infoClose.focus(); });
infoClose.addEventListener('click',()=>{ infoScrim.hidden=true; settingsToggle.focus(); });
infoScrim.addEventListener('click',ev=>{ if(ev.target===infoScrim){ infoScrim.hidden=true; settingsToggle.focus(); } });
document.addEventListener('keydown',ev=>{
  if(!infoScrim.hidden && ev.key==='Escape'){ infoScrim.hidden=true; settingsToggle.focus(); }
});
noBtn.addEventListener('click',closeAsk);
yesBtn.addEventListener('click',()=>{ closeAsk(); buzz([10,40,10]); post('/api/reset'); });
scrim.addEventListener('click',ev=>{ if(ev.target===scrim) closeAsk(); });
document.addEventListener('keydown',ev=>{
  if(scrim.hidden) return;
  if(ev.key==='Escape') closeAsk();
  if(ev.key==='Tab'){                       // keep focus inside the dialog
    const f=[noBtn,yesBtn];
    const i=f.indexOf(document.activeElement);
    ev.preventDefault();
    f[(i + (ev.shiftKey?-1:1) + f.length) % f.length].focus();
  }
});

// Show the last known scores before anything touches the network, so a cold
// open with no signal has numbers on it rather than zeros.
const cached=recall();
if(cached) adopt(cached);
render();
setSync(!!cached);

// sync rather than poll: if anything is owed it goes out first, and sync
// falls through to a plain poll when the queue is empty
sync();
setInterval(sync,2500);
</script>
</body>
</html>
"""


APPDIR = os.path.dirname(os.path.abspath(__file__))

ICONS = {
    "/icon-180.png": "icon-180.png",
    "/icon-192.png": "icon-192.png",
    "/icon-512.png": "icon-512.png",
    "/icon-512-maskable.png": "icon-512-maskable.png",
}

MANIFEST = {
    "name": "Tally",
    "short_name": "Tally",
    "description": "Cameron vs Paget",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#edf3ff",
    "theme_color": "#2563eb",
    "icons": [
        {"src": "/icon-192.png?v=" + APP_VERSION, "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png?v=" + APP_VERSION, "sizes": "512x512", "type": "image/png"},
        {"src": "/icon-512-maskable.png?v=" + APP_VERSION, "sizes": "512x512", "type": "image/png",
         "purpose": "maskable"},
    ],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tally"

    def log_message(self, fmt, *args):   # keep the container log quiet
        pass

    def _send(self, code, body, ctype, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_icon(self, name):
        path = os.path.join(APPDIR, name)
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
        except OSError:
            return self._json({"error": "icon missing"}, 404)
        # icons never change without a rename, so let phones keep them
        self._send(200, blob, "image/png", cache="public, max-age=604800")

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.replace("__APP_VERSION__", APP_VERSION), "text/html; charset=utf-8")
        elif path == "/api/state":
            with _lock:
                state = load()
                if roll_if_needed(state):
                    save(state)
                self._json(for_client(state))
        elif path == "/api/update/status":
            self._json(update_status())
        elif path in ICONS:
            self._send_icon(ICONS[path])
        elif path == "/manifest.webmanifest":
            self._send(200, json.dumps(MANIFEST), "application/manifest+json",
                       cache="public, max-age=86400")
        elif path == "/version":
            self._json({"version": APP_VERSION})
        elif path == "/sw.js":
            # no-store: a cached service worker is a service worker you cannot
            # replace, and this one is what makes the app open on a plane
            self._send(200, SW, "application/javascript; charset=utf-8",
                       cache="no-store")
        elif path == "/healthz":
            self._send(200, "ok", "text/plain; charset=utf-8")
        else:
            self._json({"error": "not found"}, 404)

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/update/check":
            self._json(check_update())
        elif path == "/api/update/install":
            ok, message = request_update()
            self._json({"ok": ok, "message": message}, 200 if ok else 409)
        elif path == "/api/add":
            body = self._read_json()
            who = str(body.get("who", ""))
            if who not in PLAYERS:
                return self._json({"error": "unknown player"}, 400)
            try:
                delta = int(body.get("delta", 1))
            except (TypeError, ValueError):
                return self._json({"error": "bad delta"}, 400)
            delta = max(-1, min(1, delta))
            with _lock:
                state = load()
                roll_if_needed(state)
                # the visible number is this month's, so that is what must not go below zero;
                # lifetime therefore cannot dip under the baseline set at the month's start
                state[who] = max(floor_for(state, who), state[who] + delta)
                save(state)
                self._json(for_client(state))
        elif path == "/api/sync":
            body = self._read_json()
            events = body.get("events")
            if not isinstance(events, list):
                return self._json({"error": "bad events"}, 400)
            with _lock:
                state = load()
                roll_if_needed(state)
                accepted = apply_events(state, events)
                save(state)
                out = for_client(state)
                out["accepted"] = accepted
                self._json(out)
        elif path == "/api/reset":
            with _lock:
                state = load()               # clears THIS MONTH only; lifetime totals survive
                roll_if_needed(state)
                state["start"] = {p: state[p] for p in PLAYERS}
                save(state)
                self._json(for_client(state))
        else:
            self._json({"error": "not found"}, 404)


def next_boundary(now=None):
    """Epoch seconds of the next 1st of the month at 00:00:00 local time."""
    lt = time.localtime(now if now is not None else time.time())
    y, m = lt.tm_year, lt.tm_mon
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    # isdst=-1 lets mktime work out daylight saving for that date itself
    return time.mktime((y, m, 1, 0, 0, 0, 0, 0, -1))


def month_watcher():
    """Close off the month the moment it ends, without waiting for someone to open the app."""
    while True:
        wait = next_boundary() - time.time()
        # wake near the boundary, but never sleep more than 5 min, so a clock
        # jump, a DST change or a suspended host can't make us miss it
        time.sleep(max(1.0, min(wait + 2, 300.0)))
        with _lock:
            state = load()
            if roll_if_needed(state):
                save(state)
                print("rolled into %s" % state["period"], flush=True)


if __name__ == "__main__":
    with _lock:
        state = load()                # make sure the data file exists and is writable
        roll_if_needed(state)
        save(state)
    threading.Thread(target=month_watcher, daemon=True).start()
    threading.Thread(target=update_check_loop, daemon=True).start()
    print("tally listening on :%s, data at %s, month %s (%s)"
          % (PORT, DATA, current_period(),
             time.tzname[1 if time.localtime().tm_isdst > 0 else 0]), flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
