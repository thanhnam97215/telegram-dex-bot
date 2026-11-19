#!/usr/bin/env python3
"""DEX bot cho Telegram: chi bao token theo chain.

Chuc nang:
  - Bao cao dau ngay (08:00 +07): top 10 tang manh nhat + top 10 giam manh
    nhat tren tung chain (mot tin nhan rieng cho moi chain).
  - Canh bao khi token bien dong >= 40% (h1 hoac h24) tren tung chain.
  - Canh bao token moi list / sap list: pool moi tren GeckoTerminal,
    token profile moi + token duoc boost tren DEX Screener (tin hieu
    cong dong dang quan tam / marketing truoc khi list).
  - Lenh Telegram: /gainers, /losers, /new, /chains, /status, /help.

Chay bang systemd (xem dexbot.service). Token Telegram chi nam trong
config.json (mode 600), khong bao gio ghi vao log hay code.
"""

import html
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

import requests

# Surrogate cho credential trong Secure Vault (token Telegram khong bao
# gio nam trong file). Thu ban he thong truoc, roi den ban vendored.
sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
try:
    from dynamic_credentials import (
        DynamicCredentialError,
        dynamic_credential_entry,
    )
except ImportError:  # fall back to the vendored copy beside this file
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from dynamic_credentials import (
        DynamicCredentialError,
        dynamic_credential_entry,
    )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
DEX_BASE = "https://api.dexscreener.com"
TG_API = "https://api.telegram.org/bot"

# Gioi han tin nhan Telegram (ky tu)
TG_LIMIT = 4000
# Giu state bao lau truoc khi cat bot
STATE_TTL_HOURS = 48

log = logging.getLogger("dexbot")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3)
    fh.setFormatter(fmt)
    log.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)


# ---------------------------------------------------------------- config
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("move_threshold_pct", 40)
    cfg.setdefault("poll_interval_sec", 600)
    cfg.setdefault("report_time", "08:00")
    cfg.setdefault("timezone", "Asia/Hovd")
    cfg.setdefault("top_n", 10)
    cfg.setdefault("min_liquidity_usd", 1000)
    cfg.setdefault("min_volume_h24_usd", 5000)
    cfg.setdefault("max_alerts_per_poll", 10)
    return cfg


# ---------------------------------------------------------------- state
def load_state():
    if not os.path.exists(STATE_PATH):
        return {"seen_pools": {}, "seen_tokens": {}, "alerts": {},
                "last_report_date": None, "update_offset": 0,
                "day_open": {}, "day_open_date": None}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("state file corrupt, starting fresh")
        return {"seen_pools": {}, "seen_tokens": {}, "alerts": {},
                "last_report_date": None, "update_offset": 0,
                "day_open": {}, "day_open_date": None}
    for k in ("seen_pools", "seen_tokens", "alerts", "day_open"):
        st.setdefault(k, {})
    st.setdefault("last_report_date", None)
    st.setdefault("update_offset", 0)
    st.setdefault("day_open_date", None)
    return st


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


def prune_state(state, now_ts):
    cutoff = now_ts - STATE_TTL_HOURS * 3600
    for key in ("seen_pools", "seen_tokens"):
        state[key] = {k: v for k, v in state[key].items() if v > cutoff}
    state["alerts"] = {k: v for k, v in state["alerts"].items()
                       if v.get("ts", 0) > cutoff}


# ---------------------------------------------------------------- helpers
def now_tz(cfg):
    return datetime.now(ZoneInfo(cfg["timezone"]))


def fmt_price(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "?"
    if p == 0:
        return "$0"
    if p >= 1000:
        return f"${p:,.2f}"
    if p >= 1:
        return f"${p:,.4f}"
    if p >= 0.01:
        return f"${p:.4f}"
    s = f"{p:.8g}"
    if "e" in s:
        # hien so thap phan day du thay vi ky hieu khoa hoc
        s = f"{p:.15f}".rstrip("0").rstrip(".")
    return f"${s}"


def fmt_usd(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def fmt_pct(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "?"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


def esc(s):
    return html.escape(str(s), quote=False)


def get_json(url, timeout=20, retries=2):
    """GET JSON voi retry don gian khi gap 429/5xx."""
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, timeout=timeout,
                             headers={"Accept": "application/json",
                                      "User-Agent": "dexbot/1.0"})
        except requests.RequestException as e:
            log.warning("GET %s failed: %s", url, e)
            time.sleep(2)
            continue
        if r.status_code == 429:
            try:
                wait = int(r.headers.get("Retry-After", "10"))
            except ValueError:
                wait = 10
            wait = max(wait, 10)
            log.warning("rate limited on %s, waiting %ss", url, wait)
            time.sleep(min(wait, 60))
            continue
        if r.status_code >= 500:
            log.warning("GET %s -> %s", url, r.status_code)
            time.sleep(3)
            continue
        if r.status_code != 200:
            log.warning("GET %s -> %s: %s", url, r.status_code,
                        r.text[:200])
            return None
        try:
            return r.json()
        except ValueError:
            log.warning("bad JSON from %s", url)
            return None
    return None


# ---------------------------------------------------------------- telegram
class Telegram:
    """Client Telegram Bot API.

    Token lay tu Secure Vault qua surrogate (custom.telegram-bot) — token
    that khong bao gio nam trong file hay log. Neu config co san
    telegram_token thi dung thang token do (che do du phong).

    Hai diem ky quac da kiem chung (19/11/2026):
    - Sentinel chi swap surrogate khi no nam o dang THO trong URL path.
      Helper url_with_surrogate_path_segment percent-encode ':' thanh %3A
      lam Sentinel khong nhan ra -> Telegram tra 404. Vi vay o day tu
      ghep URL voi surrogate tho (':' hop le trong path segment).
    - Python (urllib/requests) treo khong on dinh khi noi toi
      api.telegram.org qua egress proxy (timeout 25-60s, luc duoc luc
      khong), trong khi curl on dinh (1-7s). Vi vay dung curl qua
      subprocess cho moi goi Telegram.
    """

    def __init__(self, cfg, dry_run=False):
        self.chat_id = cfg.get("chat_id")
        self.dry_run = dry_run
        self.token = cfg.get("telegram_token") or ""
        self.credential = cfg.get("telegram_credential") or ""

    def _api_url(self, method):
        if self.token:
            return f"{TG_API}{self.token}/{method}"
        if not self.credential:
            raise DynamicCredentialError(
                "no telegram_token and no telegram_credential in config")
        entry = dynamic_credential_entry(self.credential)
        surr = str(entry["surrogate"]).strip()
        return f"https://api.telegram.org/bot{surr}/{method}"

    def _post(self, method, payload, max_time=60):
        if self.dry_run:
            text = payload.get("text", "")
            print(f"\n===== [DRY-RUN] {method} -> chat {self.chat_id} "
                  f"({len(text)} chars) =====")
            print(text)
            print("===== end =====")
            return {"ok": True}
        try:
            url = self._api_url(method)
        except DynamicCredentialError as e:
            log.error("cannot build telegram url for %s: %s", method, e)
            return None
        body = json.dumps(payload)
        cmd = ["curl", "-s", "-m", str(max_time), "-X", "POST",
               "-H", "Content-Type: application/json",
               "-d", body, url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=max_time + 10)
        except subprocess.TimeoutExpired:
            log.warning("telegram %s curl timed out", method)
            return None
        except OSError as e:
            log.warning("telegram %s curl failed: %s", method, e)
            return None
        if r.returncode != 0:
            log.warning("telegram %s curl exit %s: %s",
                        method, r.returncode, r.stderr[:200])
            return None
        try:
            data = json.loads(r.stdout)
        except ValueError:
            log.warning("telegram %s bad response: %s", method,
                        r.stdout[:200])
            return None
        if data.get("error_code") == 429:
            wait = (data.get("parameters", {}) or {}).get(
                "retry_after", 10)
            log.warning("telegram rate limited, waiting %ss", wait)
            time.sleep(wait)
            return self._post(method, payload, max_time=max_time)
        if not data.get("ok"):
            log.warning("telegram %s error: %s", method, data)
        return data

    def send(self, text, disable_preview=True):
        """Gui tin nhan, tu cat nho neu qua dai. Tra ve True neu gui duoc het."""
        chunks = []
        while len(text) > TG_LIMIT:
            cut = text.rfind("\n", 0, TG_LIMIT)
            if cut <= 0:
                cut = TG_LIMIT
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        chunks.append(text)
        ok = True
        for c in chunks:
            if not c.strip():
                continue
            res = self._post("sendMessage", {
                "chat_id": self.chat_id,
                "text": c,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            })
            if not res:
                ok = False
            time.sleep(0.3)
        return ok

    def get_updates(self, offset, timeout=30):
        if self.dry_run:
            return []
        data = self._post("getUpdates", {"offset": offset,
                                         "timeout": timeout,
                                         "allowed_updates": ["message"]},
                          max_time=timeout + 30)
        if not data or not data.get("ok"):
            return []
        return data.get("result", [])

    def get_me(self):
        if self.dry_run:
            return {"ok": True, "result": {"username": "dryrun_bot"}}
        return self._post("getMe", {})


# ---------------------------------------------------------------- data sources
def gecko_trending(network, pages=2):
    """Trending pools cua mot chain tren GeckoTerminal."""
    pools = []
    for page in range(1, pages + 1):
        url = (f"{GECKO_BASE}/networks/{network}/trending_pools"
               f"?page={page}")
        data = get_json(url)
        if not data:
            break
        pools.extend(data.get("data", []))
        if page >= 2:
            break
        time.sleep(1.0)
    return pools


def gecko_new_pools(network, pages=1):
    pools = []
    for page in range(1, pages + 1):
        url = (f"{GECKO_BASE}/networks/{network}/new_pools"
               f"?page={page}")
        data = get_json(url)
        if not data:
            break
        pools.extend(data.get("data", []))
        time.sleep(1.0)
    return pools


def dex_profiles_latest():
    data = get_json(f"{DEX_BASE}/token-profiles/latest/v1")
    return data if isinstance(data, list) else []


def dex_boosts_top():
    data = get_json(f"{DEX_BASE}/token-boosts/top/v1")
    return data if isinstance(data, list) else []


def dex_boosts_latest():
    data = get_json(f"{DEX_BASE}/token-boosts/latest/v1")
    return data if isinstance(data, list) else []


def dex_pairs_by_tokens(chain_id, addresses):
    """Lay thong tin pairs cho nhieu token cung chain (toi da 30 dia chi)."""
    if not addresses:
        return []
    joined = ",".join(addresses[:30])
    data = get_json(f"{DEX_BASE}/latest/dex/tokens/{joined}")
    if not data or not isinstance(data, dict):
        return []
    pairs = data.get("pairs", []) or []
    return [p for p in pairs if p.get("chainId") == chain_id]


def pool_view(pool):
    """Rut gon mot GeckoTerminal pool ve cac truong can dung."""
    a = pool.get("attributes", {}) or {}
    pct = a.get("price_change_percentage", {}) or {}
    vol = a.get("volume_usd", {}) or {}
    tx = a.get("transactions", {}) or {}
    h24tx = tx.get("h24", {}) or {}
    name = a.get("name", "?")
    base = name.split(" / ")[0] if " / " in name else name
    # dia chi base token (de gop cac pool trung token)
    base_addr = ""
    try:
        rel = (pool.get("relationships", {}) or {}).get("base_token", {})
        tid = (rel.get("data", {}) or {}).get("id", "")
        base_addr = tid.split("_", 1)[1] if "_" in tid else tid
    except (AttributeError, IndexError):
        base_addr = ""

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "id": pool.get("id", ""),
        "address": a.get("address", ""),
        "base_addr": base_addr,
        "name": name,
        "base": base,
        "price_usd": a.get("base_token_price_usd"),
        "h1": _f(pct.get("h1")),
        "h6": _f(pct.get("h6")),
        "h24": _f(pct.get("h24")),
        "vol_h24": vol.get("h24"),
        "reserve_usd": a.get("reserve_in_usd"),
        "fdv_usd": a.get("fdv_usd"),
        "mcap_usd": a.get("market_cap_usd"),
        "created_at": a.get("pool_created_at"),
        "buys_h24": h24tx.get("buys"),
        "sells_h24": h24tx.get("sells"),
        "traders_h24": h24tx.get("unique_traders"),
    }


# ---------------------------------------------------------------- report
def gecko_chain_label(chain):
    return chain.get("label", chain.get("id", "?"))


def pool_line(i, v, chain_dex_id):
    """Mot dong trong bang top: hang, ten, % h24, gia, vol, link."""
    name = esc(v["base"])
    pct = fmt_pct(v["h24"])
    arrow = "🟢" if (v["h24"] or 0) >= 0 else "🔴"
    url = (f"https://dexscreener.com/{chain_dex_id}/{v['address']}"
           if v["address"] else None)
    link = f'<a href="{url}">{name}</a>' if url else name
    vol = fmt_usd(v["vol_h24"])
    liq = fmt_usd(v["reserve_usd"])
    return (f"{i}. {arrow} {link}  <b>{pct}</b>\n"
            f"     {fmt_price(v['price_usd'])} · Vol 24h {vol} · "
            f"Liq {liq}")


def build_chain_report(cfg, chain, pools):
    """Bao cao top tang/giam cua mot chain (GeckoTerminal trending)."""
    label = gecko_chain_label(chain)
    top_n = cfg["top_n"]
    min_liq = cfg["min_liquidity_usd"]
    min_vol = cfg["min_volume_h24_usd"]
    views = []
    for p in pools:
        v = pool_view(p)
        try:
            h24 = float(v["h24"])
        except (TypeError, ValueError):
            continue
        try:
            liq = float(v["reserve_usd"] or 0)
            vol = float(v["vol_h24"] or 0)
        except (TypeError, ValueError):
            liq, vol = 0, 0
        if liq < min_liq or vol < min_vol:
            continue
        v["_h24"] = h24
        v["_vol"] = vol
        views.append(v)
    # Gop cac pool trung token: giu pool co volume cao nhat
    best = {}
    for v in views:
        key = (v.get("base_addr") or v["base"]).lower()
        if key not in best or v["_vol"] > best[key]["_vol"]:
            best[key] = v
    views = list(best.values())
    if not views:
        return (f"<b>📊 {esc(label)}</b>\n"
                f"Khong co pool nao du thanh khoan / volume de xep hang "
                f"hom nay.")
    gainers = sorted(views, key=lambda v: v["_h24"], reverse=True)[:top_n]
    losers = sorted(views, key=lambda v: v["_h24"])[:top_n]
    today = now_tz(cfg).strftime("%d/%m/%Y")
    dex_id = chain.get("dex", chain["id"])
    lines = [f"<b>📊 {esc(label)} — Top {top_n} ({today})</b>",
             f"<i>Trong cac pool dang hot tren GeckoTerminal</i>", ""]
    lines.append(f"<b>🟢 Tang manh nhat 24h</b>")
    for i, v in enumerate(gainers, 1):
        lines.append(pool_line(i, v, dex_id))
    lines.append("")
    lines.append(f"<b>🔴 Giam manh nhat 24h</b>")
    for i, v in enumerate(losers, 1):
        lines.append(pool_line(i, v, dex_id))
    return "\n".join(lines)


def build_robinhood_report(cfg, chain, boosts):
    """Robinhood Chain khong co tren GeckoTerminal: dung top token duoc
    boost tren DEX Screener + bien dong h24 cua chung."""
    label = gecko_chain_label(chain)
    today = now_tz(cfg).strftime("%d/%m/%Y")
    dex_id = chain.get("dex", chain["id"])
    items = [b for b in boosts if b.get("chainId") == dex_id]
    if not items:
        return (f"<b>📊 {esc(label)} — Top {cfg['top_n']} ({today})</b>\n"
                f"Chua co token nao duoc boost tren DEX Screener hom nay.")
    addrs = [b.get("tokenAddress") for b in items[:30]
             if b.get("tokenAddress")]
    pairs = dex_pairs_by_tokens(dex_id, addrs)
    by_addr = {}
    for p in pairs:
        addr = (p.get("baseToken") or {}).get("address", "")
        if addr and addr not in by_addr:
            by_addr[addr] = p
    rows = []
    for b in items:
        addr = b.get("tokenAddress", "")
        p = by_addr.get(addr) or by_addr.get(addr.lower(), {})
        pc = (p.get("priceChange") or {}).get("h24")
        try:
            h24 = float(pc)
        except (TypeError, ValueError):
            h24 = None
        sym = ((p.get("baseToken") or {}).get("symbol")
               or b.get("description", "?")[:20] or "?")
        rows.append({
            "sym": sym,
            "h24": h24,
            "price": p.get("priceUsd"),
            "vol": (p.get("volume") or {}).get("h24"),
            "liq": (p.get("liquidity") or {}).get("usd"),
            "url": b.get("url"),
            "boost": b.get("totalAmount"),
        })
    rows.sort(key=lambda r: (r["h24"] is None,
                             -(r["h24"] or 0)))
    lines = [f"<b>📊 {esc(label)} — Token dang duoc quan tam "
             f"({today})</b>",
             f"<i>Top token duoc boost tren DEX Screener (GeckoTerminal "
             f"chua ho tro chain nay)</i>", ""]
    for i, r in enumerate(rows[:cfg["top_n"]], 1):
        pct = fmt_pct(r["h24"]) if r["h24"] is not None else "?"
        arrow = ("🟢" if (r["h24"] or 0) >= 0 else "🔴")
        name = f'<a href="{r["url"]}">{esc(r["sym"])}</a>' \
            if r["url"] else esc(r["sym"])
        lines.append(f"{i}. {arrow} {name}  <b>{pct}</b>")
        lines.append(f"     {fmt_price(r['price'])} · Vol 24h "
                     f"{fmt_usd(r['vol'])} · Boost ${r['boost'] or '?'}")
    return "\n".join(lines)


def do_report(cfg, tg, state):
    """Gui bao cao dau ngay: mot tin nhan cho moi chain."""
    chains = cfg["chains"]
    boosts = None
    for chain in chains:
        try:
            if chain.get("gecko"):
                pools = gecko_trending(chain["gecko"], pages=2)
                text = build_chain_report(cfg, chain, pools)
            else:
                if boosts is None:
                    boosts = dex_boosts_top()
                text = build_robinhood_report(cfg, chain, boosts)
            tg.send(text)
            log.info("report sent for chain %s", chain["id"])
        except Exception as e:
            log.exception("report failed for chain %s: %s", chain["id"], e)
        time.sleep(1)
    state["last_report_date"] = now_tz(cfg).strftime("%Y-%m-%d")
    save_state(state)


# ---------------------------------------------------------------- poll: movers
def check_movers(cfg, tg, state, now_ts):
    """Canh bao token bien dong >= nguong (h1 hoac h24)."""
    threshold = cfg["move_threshold_pct"]
    max_alerts = cfg["max_alerts_per_poll"]
    alerts = []
    for chain in cfg["chains"]:
        if not chain.get("gecko"):
            continue
        try:
            pools = gecko_trending(chain["gecko"], pages=2)
        except Exception as e:
            log.warning("trending %s failed: %s", chain["id"], e)
            continue
        for p in pools:
            v = pool_view(p)
            try:
                h1 = float(v["h1"]) if v["h1"] is not None else 0
                h24 = float(v["h24"]) if v["h24"] is not None else 0
            except (TypeError, ValueError):
                continue
            move = None
            window = None
            if h24 >= threshold:
                move, window = h24, "24h"
            elif h24 <= -threshold:
                move, window = h24, "24h"
            elif h1 >= threshold:
                move, window = h1, "1h"
            elif h1 <= -threshold:
                move, window = h1, "1h"
            if move is None:
                continue
            direction = "up" if move > 0 else "down"
            token_key = v.get("base_addr") or v["id"]
            key = f"{chain['id']}:{token_key}:{direction}"
            prev = state["alerts"].get(key)
            if prev and now_ts - prev.get("ts", 0) < 24 * 3600:
                continue
            alerts.append((abs(move), chain, v, move, window, key))
        time.sleep(0.5)
    alerts.sort(key=lambda a: a[0], reverse=True)
    sent = 0
    for _, chain, v, move, window, key in alerts[:max_alerts]:
        label = gecko_chain_label(chain)
        dex_id = chain.get("dex", chain["id"])
        url = (f"https://dexscreener.com/{dex_id}/{v['address']}"
               if v["address"] else None)
        name = (f'<a href="{url}">{esc(v["base"])}</a>' if url
                else esc(v["base"]))
        emoji = "🚀" if move > 0 else "📉"
        verb = "TANG" if move > 0 else "GIAM"
        text = (
            f"{emoji} <b>BIEN DONG MANH — {esc(label)}</b>\n"
            f"{name} {verb} <b>{fmt_pct(move)}</b> ({window})\n"
            f"Gia: {fmt_price(v['price_usd'])} · "
            f"24h: {fmt_pct(v['h24'])} · 1h: {fmt_pct(v['h1'])}\n"
            f"Vol 24h: {fmt_usd(v['vol_h24'])} · "
            f"Liq: {fmt_usd(v['reserve_usd'])} · "
            f"FDV: {fmt_usd(v['fdv_usd'])}\n"
            f"Mua/Ban 24h: {v['buys_h24'] or '?'} / {v['sells_h24'] or '?'}"
        )
        if tg.send(text):
            state["alerts"][key] = {"ts": now_ts}
            sent += 1
        time.sleep(0.5)
    if sent:
        log.info("sent %d mover alerts", sent)
    return sent


# ---------------------------------------------------------------- poll: new listings
def check_new_pools(cfg, tg, state, now_ts):
    """Pool moi vua tao tren tung chain (GeckoTerminal new pools)."""
    sent = 0
    max_new = cfg.get("max_new_per_poll", 5)
    for chain in cfg["chains"]:
        if not chain.get("gecko"):
            continue
        try:
            pools = gecko_new_pools(chain["gecko"], pages=1)
        except Exception as e:
            log.warning("new pools %s failed: %s", chain["id"], e)
            continue
        for p in pools:
            v = pool_view(p)
            key = f"{chain['id']}:{v['id']}"
            if key in state["seen_pools"]:
                continue
            # Chi bao pool that su moi (tao trong 2h qua); pool cu hon
            # chi danh dau da thay de lan quet sau khoi bao lai
            try:
                created_dt = datetime.fromisoformat(
                    (v["created_at"] or "").replace("Z", "+00:00"))
                age_sec = (datetime.now(timezone.utc)
                           - created_dt).total_seconds()
            except (ValueError, TypeError):
                age_sec = 0
            if age_sec > 2 * 3600:
                state["seen_pools"][key] = now_ts
                continue
            if sent >= max_new:
                continue
            created = v["created_at"] or "?"
            try:
                created_dt = datetime.fromisoformat(
                    created.replace("Z", "+00:00"))
                age_min = int((datetime.now(timezone.utc)
                               - created_dt).total_seconds() // 60)
                age = f"{age_min} phut truoc" if age_min < 60 \
                    else f"{age_min // 60}h{age_min % 60:02d} truoc"
            except (ValueError, TypeError):
                age = created
            label = gecko_chain_label(chain)
            dex_id = chain.get("dex", chain["id"])
            url = (f"https://dexscreener.com/{dex_id}/{v['address']}"
                   if v["address"] else None)
            name = (f'<a href="{url}">{esc(v["base"])}</a>' if url
                    else esc(v["base"]))
            text = (
                f"🆕 <b>Pool moi — {esc(label)}</b>\n"
                f"{name} ({esc(v['name'])})\n"
                f"Tao cach day: {age}\n"
                f"Gia: {fmt_price(v['price_usd'])} · "
                f"Liq: {fmt_usd(v['reserve_usd'])} · "
                f"FDV: {fmt_usd(v['fdv_usd'])}"
            )
            if tg.send(text):
                state["seen_pools"][key] = now_ts
                sent += 1
            time.sleep(0.5)
        time.sleep(0.5)
    if sent:
        log.info("sent %d new-pool alerts", sent)
    return sent


def check_dex_profiles(cfg, tg, state, now_ts):
    """Token profile moi + boost moi tren DEX Screener: tin hieu token
    sap list / vua list va dang duoc cong dong chu y."""
    dex_ids = {c.get("dex", c["id"]) for c in cfg["chains"]}
    labels = {c.get("dex", c["id"]): gecko_chain_label(c)
              for c in cfg["chains"]}
    sent = 0
    max_new = cfg.get("max_new_per_poll", 5)
    items = []
    for it in dex_profiles_latest():
        it = dict(it)
        it["_kind"] = "profile"
        items.append(it)
    for it in dex_boosts_latest():
        it = dict(it)
        it["_kind"] = "boost"
        items.append(it)
    for it in items:
        chain_id = it.get("chainId")
        addr = it.get("tokenAddress", "")
        if chain_id not in dex_ids or not addr:
            continue
        key = f"{chain_id}:{addr.lower()}"
        if key in state["seen_tokens"]:
            continue
        if sent >= max_new:
            continue
        label = labels.get(chain_id, chain_id)
        url = it.get("url") or ""
        desc = (it.get("description") or "").strip()
        if len(desc) > 200:
            desc = desc[:200] + "…"
        links = []
        for ln in it.get("links", []) or []:
            t = ln.get("type", "web")
            u = ln.get("url", "")
            if u:
                links.append(f'<a href="{u}">{esc(t)}</a>')
        kind = ("📣 Token moi co profile" if it["_kind"] == "profile"
                else "🔥 Token moi duoc boost")
        name = (f'<a href="{url}">{esc(addr[:10])}…</a>' if url
                else esc(addr))
        boost_amt = it.get("totalAmount")
        text = f"{kind} — <b>{esc(label)}</b>\nToken: {name}\n"
        if desc:
            text += f"{esc(desc)}\n"
        if boost_amt:
            text += f"Boost: ${boost_amt}\n"
        if links:
            text += "Link: " + " · ".join(links) + "\n"
        text += (f"<i>Dia chi: <code>{esc(addr)}</code></i>")
        if tg.send(text):
            state["seen_tokens"][key] = now_ts
            sent += 1
        time.sleep(0.5)
    if sent:
        log.info("sent %d dex profile/boost alerts", sent)
    return sent


def do_poll(cfg, tg, state):
    now_ts = time.time()
    prune_state(state, now_ts)
    n1 = check_movers(cfg, tg, state, now_ts)
    n2 = check_new_pools(cfg, tg, state, now_ts)
    n3 = check_dex_profiles(cfg, tg, state, now_ts)
    save_state(state)
    log.info("poll done: movers=%d new_pools=%d profiles=%d",
             n1, n2, n3)
    return n1 + n2 + n3


# ---------------------------------------------------------------- commands
HELP_TEXT = (
    "<b>DEX Bot — chi bao token theo chain</b>\n\n"
    "/gainers [chain] — top 10 tang manh nhat 24h\n"
    "/losers [chain] — top 10 giam manh nhat 24h\n"
    "/new [chain] — pool moi nhat + token moi co profile/boost\n"
    "/chains — cac chain dang theo doi\n"
    "/status — trang thai bot\n"
    "/help — huong dan nay\n\n"
    "Vi du: <code>/gainers bsc</code>, <code>/losers eth</code>, "
    "<code>/new solana</code>\n"
    "Khong ghi chain thi mac dinh hien BSC.\n\n"
    "Bot tu dong: bao cao top 10 dau ngay (08:00), canh bao bien "
    "dong &gt;= 40%, canh bao token moi list / sap list."
)


def find_chain(cfg, arg):
    if not arg:
        return None
    arg = arg.lower().strip()
    for c in cfg["chains"]:
        if arg in (c["id"], c.get("dex", ""), c.get("gecko", ""),
                   c.get("label", "").lower()):
            return c
    return None


def default_chain(cfg):
    for c in cfg["chains"]:
        if c["id"] == "bsc":
            return c
    return cfg["chains"][0]


def cmd_gainers_losers(cfg, tg, chain, gainers=True):
    if not chain.get("gecko"):
        tg.send(f"Chain {esc(gecko_chain_label(chain))} chua co tren "
                f"GeckoTerminal nen chua xep hang tang/giam duoc. "
                f"Dung /new de xem token moi tren chain nay.")
        return
    pools = gecko_trending(chain["gecko"], pages=2)
    text = build_chain_report(cfg, chain, pools)
    # Chi lay mot nua phu hop (tang hoac giam)
    if gainers:
        text = text.split("<b>🔴 Giam manh nhat 24h</b>")[0].rstrip()
    else:
        head, _, tail = text.partition("<b>🔴 Giam manh nhat 24h</b>")
        title = head.split("\n")[0]
        text = title + "\n\n<b>🔴 Giam manh nhat 24h</b>" + tail
    tg.send(text)


def cmd_new(cfg, tg, chain):
    label = gecko_chain_label(chain)
    lines = [f"<b>🆕 Moi nhat — {esc(label)}</b>", ""]
    if chain.get("gecko"):
        pools = gecko_new_pools(chain["gecko"], pages=1)
        dex_id = chain.get("dex", chain["id"])
        for v in [pool_view(p) for p in pools[:10]]:
            url = (f"https://dexscreener.com/{dex_id}/{v['address']}"
                   if v["address"] else None)
            name = (f'<a href="{url}">{esc(v["base"])}</a>' if url
                    else esc(v["base"]))
            lines.append(
                f"• {name} — {fmt_price(v['price_usd'])} · "
                f"Liq {fmt_usd(v['reserve_usd'])} · tao "
                f"{(v['created_at'] or '?')[:16].replace('T', ' ')}")
    dex_id = chain.get("dex", chain["id"])
    profs = [p for p in dex_profiles_latest()
             if p.get("chainId") == dex_id][:5]
    if profs:
        lines.append("")
        lines.append("<b>Token moi co profile tren DEX Screener:</b>")
        for p in profs:
            addr = p.get("tokenAddress", "")
            url = p.get("url") or ""
            name = (f'<a href="{url}">{esc(addr[:10])}…</a>' if url
                    else esc(addr))
            desc = (p.get("description") or "")[:80]
            lines.append(f"• {name} — {esc(desc)}")
    if len(lines) <= 2:
        lines.append("Chua co du lieu moi.")
    tg.send("\n".join(lines))


def cmd_chains(cfg, tg):
    lines = ["<b>⛓️ Cac chain dang theo doi</b>", ""]
    for c in cfg["chains"]:
        gecko = "✅" if c.get("gecko") else "⚠️ (chi profile/boost)"
        lines.append(f"• <b>{esc(gecko_chain_label(c))}</b> "
                     f"(<code>{c['id']}</code>) {gecko}")
    lines.append("")
    lines.append(f"Nguong canh bao bien dong: "
                 f"{cfg['move_threshold_pct']}% · Bao cao dau ngay: "
                 f"{cfg['report_time']} ({cfg['timezone']})")
    tg.send("\n".join(lines))


def cmd_status(cfg, tg, state):
    now = now_tz(cfg)
    lines = [
        "<b>🤖 Trang thai bot</b>",
        f"Gio hien tai: {now.strftime('%d/%m/%Y %H:%M')}",
        f"Bao cao dau ngay: "
        f"{state.get('last_report_date') or 'chua gui'}",
        f"Pool da thay: {len(state.get('seen_pools', {}))} · "
        f"Token da thay: {len(state.get('seen_tokens', {}))} · "
        f"Canh bao da gui: {len(state.get('alerts', {}))}",
        f"Chu ky quet: {cfg['poll_interval_sec'] // 60} phut · "
        f"Nguong: {cfg['move_threshold_pct']}%",
    ]
    tg.send("\n".join(lines))


def handle_message(cfg, tg, state, msg):
    chat_id = (msg.get("chat") or {}).get("id")
    if str(chat_id) != str(cfg["chat_id"]):
        log.info("ignoring message from unknown chat %s", chat_id)
        return
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        tg.send("Minh la bot chi bao DEX. Go /help de xem cac lenh.")
        return
    parts = text[1:].split()
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else None
    log.info("command /%s %s", cmd, arg or "")
    try:
        if cmd in ("start", "help"):
            tg.send(HELP_TEXT)
        elif cmd in ("gainers", "losers"):
            chain = find_chain(cfg, arg) or default_chain(cfg)
            cmd_gainers_losers(cfg, tg, chain, gainers=(cmd == "gainers"))
        elif cmd == "new":
            chain = find_chain(cfg, arg) or default_chain(cfg)
            cmd_new(cfg, tg, chain)
        elif cmd == "chains":
            cmd_chains(cfg, tg)
        elif cmd == "status":
            cmd_status(cfg, tg, state)
        else:
            tg.send("Lenh chua biet. Go /help de xem cac lenh.")
    except Exception as e:
        log.exception("command /%s failed: %s", cmd, e)
        tg.send("Gap loi khi xu ly lenh, thu lai sau chut nhe.")


def do_commands(cfg, tg, state, timeout=5):
    """Xu ly cac lenh dang cho (getUpdates)."""
    updates = tg.get_updates(state.get("update_offset", 0),
                             timeout=timeout)
    for u in updates:
        state["update_offset"] = u["update_id"] + 1
        msg = u.get("message")
        if msg:
            handle_message(cfg, tg, state, msg)
    if updates:
        save_state(state)
    return len(updates)


# ---------------------------------------------------------------- main loop
def report_due(cfg, state):
    today = now_tz(cfg).strftime("%Y-%m-%d")
    if state.get("last_report_date") == today:
        return False
    hh, mm = cfg["report_time"].split(":")
    now = now_tz(cfg)
    return (now.hour, now.minute) >= (int(hh), int(mm))


def run_daemon(cfg, tg):
    state = load_state()
    log.info("dexbot started (chat_id=%s, chains=%s)",
             cfg["chat_id"], [c["id"] for c in cfg["chains"]])
    me = tg.get_me()
    if me and me.get("ok"):
        log.info("bot username: @%s",
                 me.get("result", {}).get("username"))
    else:
        log.warning("getMe failed - kiem tra lai token")
    last_poll = 0
    while True:
        try:
            now_ts = time.time()
            # Lenh den: kiem tra thuong xuyen (long polling ngan)
            do_commands(cfg, tg, state, timeout=5)
            # Bao cao dau ngay
            if report_due(cfg, state):
                log.info("daily report due, sending")
                do_report(cfg, tg, state)
            # Quet dinh ky
            if now_ts - last_poll >= cfg["poll_interval_sec"]:
                last_poll = now_ts
                do_poll(cfg, tg, state)
        except Exception as e:
            log.exception("main loop error: %s", e)
        time.sleep(5)


def main():
    setup_logging()
    if len(sys.argv) < 2:
        print("usage: dexbot.py run|report|poll|commands|test "
              "[--dry-run]")
        sys.exit(2)
    action = sys.argv[1]
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    if not dry_run:
        if not (cfg.get("telegram_token")
                or cfg.get("telegram_credential")):
            print("config.json chua co telegram_token hay "
                  "telegram_credential (Secure Vault)")
            sys.exit(2)
        if action not in ("test", "chatid") and not cfg.get("chat_id"):
            print("config.json chua co chat_id (nhan /start cho bot "
                  "roi lay chat id tu getUpdates)")
            sys.exit(2)
    tg = Telegram(cfg, dry_run=dry_run)
    state = load_state()
    if action == "run":
        run_daemon(cfg, tg)
    elif action == "report":
        do_report(cfg, tg, state)
    elif action == "poll":
        do_poll(cfg, tg, state)
    elif action == "commands":
        n = do_commands(cfg, tg, state, timeout=5)
        print(f"handled {n} updates")
    elif action == "chatid":
        # Liet ke cac chat da nhan tin cho bot (de lay chat_id khi cai
        # dat lan dau). Khong can chat_id trong config de chay.
        updates = tg.get_updates(0, timeout=5)
        seen = {}
        for u in updates:
            msg = u.get("message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid is None:
                continue
            seen[cid] = {
                "type": chat.get("type"),
                "username": chat.get("username"),
                "first_name": chat.get("first_name"),
                "last_text": (msg.get("text") or "")[:60],
            }
        if not seen:
            print("chua co ai nhan tin cho bot (bao Ronald /start bot)")
        for cid, info in seen.items():
            print(f"chat_id={cid} {info}")
    elif action == "test":
        me = tg.get_me()
        print("getMe:", me)
        if me and me.get("ok"):
            if cfg.get("chat_id"):
                tg.send("✅ DEX Bot da ket noi xong! Go /help de xem "
                        "cac lenh.")
                print("test message sent")
            else:
                print("getMe OK, chua co chat_id nen chua gui tin "
                      "nhan thu")
    else:
        print(f"unknown action: {action}")
        sys.exit(2)


if __name__ == "__main__":
    main()
