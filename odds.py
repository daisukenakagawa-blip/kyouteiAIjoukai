"""公式サイト（www.boatrace.jp）から直前情報とオッズを取得する。リクエストは1秒以上あける。

fetch_beforeinfo(date, venue, race) -> {exhibit: {艇番: 展示タイム}, absent: [艇番], wind_speed, wave, weather, deadline}
fetch_odds(date, venue, race)       -> {"単勝": {"1": 1.2}, "複勝": {"1": 1.0}, "2連単": {"1-2": 4.1}, "2連複", "3連単", "3連複"}
複勝は「1.0-1.1」の下限を使う（期待値を控えめに見るため）。
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
BASE = "https://www.boatrace.jp/owpc/pc/race/{page}?rno={r}&jcd={v:02d}&hd={d:%Y%m%d}"
UA = "Mozilla/5.0 (personal boatrace prediction)"
_lock = threading.Lock()
_next = [0.0]


def _slot():
    """全スレッド合計で、リクエスト開始を1秒以上あける。"""
    with _lock:
        now = time.monotonic()
        t = max(now, _next[0])
        _next[0] = t + 1.0
    time.sleep(max(0.0, t - now))


def get(page, d, v, r) -> BeautifulSoup:
    url = BASE.format(page=page, r=r, v=v, d=d)
    for i in range(3):
        _slot()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as res:
                html = res.read().decode("utf-8", errors="replace")
            return BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"取得できませんでした: {url}")


def fetch_race(d, v, r):
    """直前情報と全オッズのページ5つを並行して取り、(before, odds) を返す。"""
    pages = ["beforeinfo", "oddstf", "odds2tf", "odds3t", "odds3f"]
    with ThreadPoolExecutor(max_workers=5) as ex:
        soups = dict(zip(pages, ex.map(lambda p: get(p, d, v, r), pages)))
    return parse_beforeinfo(soups["beforeinfo"], r), parse_odds(soups)


def _num(s):
    m = re.search(r"\d+(\.\d+)?", s or "")
    return float(m.group(0)) if m else None


def fetch_beforeinfo(d, v, r) -> dict:
    return parse_beforeinfo(get("beforeinfo", d, v, r), r)


def parse_beforeinfo(s, r) -> dict:
    out = {"exhibit": {}, "absent": []}
    t = s.select_one("table.is-w748")
    lanes_seen = []
    if t:
        for tb in t.select("tbody"):
            tds = tb.select_one("tr").select("td")
            if len(tds) < 5 or not tds[0].get_text(strip=True).isdigit():
                continue
            lane = int(tds[0].get_text(strip=True))
            lanes_seen.append(lane)
            ex = _num(tds[4].get_text(strip=True))
            if ex and 6.0 < ex < 8.0:
                out["exhibit"][str(lane)] = ex
            if "is-miss" in (tb.get("class") or []):
                out["absent"].append(lane)
    # 展示が一部の艇だけ空なら、その艇は欠場とみなす（全艇空ならまだ展示前）
    if out["exhibit"]:
        out["absent"] = sorted(set(out["absent"]) | {l for l in lanes_seen if str(l) not in out["exhibit"]})
    for u in s.select(".weather1_bodyUnit"):
        cls = u.get("class") or []
        txt = u.get_text(" ", strip=True)
        if "is-wind" in cls:
            out["wind_speed"] = _num(txt)
        elif "is-wave" in cls:
            out["wave"] = _num(txt)
        elif "is-weather" in cls:
            out["weather"] = txt
    out["deadline"] = deadline(s, r)
    return out


def fetch_deadlines(d, v) -> dict:
    """その場の全レースの締切予定時刻 {R: "HH:MM"}（1リクエスト）。"""
    s = get("beforeinfo", d, v, 1)
    for tr in s.select("table tr"):
        cells = [c.get_text(strip=True) for c in tr.select("td, th")]
        if cells and cells[0].startswith("締切予定時刻"):
            return {i: t for i, t in enumerate(cells[1:], start=1) if re.match(r"^\d{1,2}:\d{2}$", t)}
    return {}


def deadline(s, r):
    for tr in s.select("table tr"):
        cells = [c.get_text(strip=True) for c in tr.select("td, th")]
        if cells and cells[0].startswith("締切予定時刻") and len(cells) > r:
            return cells[r]
    return None


def _segments(tr):
    """1行を「…, オッズ」の区切りに分ける。無効セル(is-disabled)は飛ばす。"""
    segs, cur = [], []
    for td in tr.select("td"):
        cls = td.get("class") or []
        if "is-disabled" in cls:
            continue
        txt = td.get_text(strip=True)
        if "oddsPoint" in cls:
            segs.append((cur, txt))
            cur = []
        else:
            cur.append(txt)
    return segs


def _parse_grid(table, kind):
    """2連単/2連複/3連単/3連複の表。列=1着(1つ目)の艇、行ごとに2着(・3着)が並ぶ。"""
    res = {}
    second = {}  # 3連系: 列ごとの2着（rowspanで省略される）
    for tr in table.select("tbody tr"):
        for col, (vals, odds) in enumerate(_segments(tr)):
            o = _num(odds)
            if not o:  # 0.0 = まだ票が入っていない
                continue
            first = col + 1
            if kind in ("2連単", "2連複"):
                if not vals:
                    continue
                combo = (first, int(vals[-1]))
            else:
                if len(vals) >= 2:
                    second[col] = int(vals[-2])
                if not vals or col not in second:
                    continue
                combo = (first, second[col], int(vals[-1]))
            if kind in ("2連複", "3連複"):
                combo = tuple(sorted(combo))
            res["-".join(map(str, combo))] = o
    return res


def fetch_odds(d, v, r) -> dict:
    return parse_odds({p: get(p, d, v, r) for p in ["oddstf", "odds2tf", "odds3t", "odds3f"]})


def parse_odds(soups) -> dict:
    out = {}
    s = soups["oddstf"]
    tabs = s.select("table.is-w495")
    for t, kind in zip(tabs[:2], ["単勝", "複勝"]):
        res = {}
        for tr in t.select("tbody tr"):
            tds = tr.select("td")
            if not tds or not tds[0].get_text(strip=True).isdigit():
                continue
            pt = tr.select_one(".oddsPoint")
            o = _num(pt.get_text(strip=True)) if pt else None  # 複勝は下限
            if o:
                res[tds[0].get_text(strip=True)] = o
        out[kind] = res
    s = soups["odds2tf"]
    tabs = [t for t in s.select("table") if t.select(".oddsPoint")]
    if len(tabs) >= 2:
        out["2連単"] = _parse_grid(tabs[0], "2連単")
        out["2連複"] = _parse_grid(tabs[1], "2連複")
    for page, kind in [("odds3t", "3連単"), ("odds3f", "3連複")]:
        s = soups[page]
        tabs = [t for t in s.select("table") if t.select(".oddsPoint")]
        out[kind] = _parse_grid(tabs[0], kind) if tabs else {}
    return out


RESULT_BETS = {"単勝", "複勝", "2連単", "2連複", "拡連複", "3連単", "3連複"}


def fetch_result(d, v, r):
    """確定結果: {"order": [1着の艇, 2着, 3着, ...], "pay": {"3連単": {"5-1-3": 55440}, ...}}。未確定なら None。"""
    s = get("raceresult", d, v, r)
    order = []
    pay = {}
    for t in s.select("table.is-w495"):
        txt = t.get_text()
        if "勝式" in txt:
            cur = None
            for tr in t.select("tbody tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.select("td")]
                if not any(cells):
                    continue
                if cells[0] in RESULT_BETS:
                    cur, cells = cells[0], cells[1:]
                if not cur or len(cells) < 2:
                    continue
                lanes = [int(x) for x in re.findall(r"\d", cells[0])]
                amt = re.sub(r"[^0-9]", "", cells[1])
                if not lanes or not amt:
                    continue
                if cur in ("2連複", "3連複", "拡連複"):
                    lanes = sorted(lanes)
                pay.setdefault(cur, {})["-".join(map(str, lanes))] = int(amt)
        elif "ボートレーサー" in txt and "着" in txt and not order:
            for tr in t.select("tbody tr"):
                tds = [td.get_text(strip=True) for td in tr.select("td")]
                if len(tds) >= 2 and tds[1].isdigit():
                    order.append(int(tds[1]))
    if not pay:
        return None
    return {"order": order, "pay": pay}
