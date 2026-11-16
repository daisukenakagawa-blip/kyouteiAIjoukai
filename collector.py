"""直前オッズの自動収集（PC不要・クラウドで動かす用）。モデルは使わず、公式サイトの直前情報とオッズを保存するだけ。

    python collector.py --hours 5.5 --before 5 1      # 各レースの締切5分前と1分前に取得、5.5時間で終了
依存: beautifulsoup4, lxml（odds.py と同じフォルダに置く）
保存: data/odds/YYYYMMDD/odds_VV_RR_HHMMSS.json（取得時刻・締切までの分数・直前情報・全券種オッズ）
      data/odds/YYYYMMDD/fetch_log.jsonl（成功・失敗・再試行をすべて1行ずつ記録）
アクセスは全体で1秒に1件まで。締切が重なって間に合わない取得は「skipped_late」として記録し、無理に追いかけない。
"""
import argparse, datetime as dt, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import odds as om

JST = dt.timezone(dt.timedelta(hours=9))
now = lambda: dt.datetime.now(JST).replace(tzinfo=None)


def log(day_dir, **kw):
    kw["at"] = now().isoformat(timespec="seconds")
    with open(day_dir / "fetch_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(kw, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=5.5)
    ap.add_argument("--before", type=float, nargs="+", default=[5, 1])
    ap.add_argument("--out", default="data/odds")
    a = ap.parse_args()
    end = now() + dt.timedelta(hours=a.hours)
    d = now().date()
    day = Path(a.out) / f"{d:%Y%m%d}"
    day.mkdir(parents=True, exist_ok=True)
    sched = []
    def deadlines(v):
        for attempt in (1, 2):
            try:
                dl = om.fetch_deadlines(d, v)
                log(day, kind="deadlines", venue=v, races=len(dl), attempt=attempt, ok=True)
                return v, dl
            except Exception as e:  # noqa: BLE001
                log(day, kind="deadlines", venue=v, attempt=attempt, ok=False, error=str(e)[:200])
        return v, {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:     # 全国24場の締切時刻（開始間隔は全体で1秒に1件のまま）
        allv = list(ex.map(deadlines, range(1, 25)))
    for v, dl in allv:
        for r, t in dl.items():
            h, m = map(int, t.split(":"))
            close = dt.datetime.combine(d, dt.time(h, m))
            for b in a.before:
                at = close - dt.timedelta(minutes=b)
                if now() < at < end:
                    sched.append((at, v, r, close, b))
    sched.sort()
    (day / "schedule.json").write_text(json.dumps([[s[0].isoformat(), s[1], s[2], s[4]] for s in sched]))
    print(f"{len(sched)} 回の取得を予定", flush=True)
    for at, v, r, close, b in sched:
        wait = (at - now()).total_seconds()
        if wait > 0:
            time.sleep(wait)
        if now() >= close - dt.timedelta(seconds=20):   # 前の取得が長引いて締切に間に合わない
            log(day, kind="odds", venue=v, race=r, target_before=b, ok=False, error="skipped_late")
            continue
        for attempt in (1, 2):
            t0 = now()
            try:
                before, odds = om.fetch_race(d, v, r)
                t1 = now()
                mins = (close - t1).total_seconds() / 60
                if mins <= 0:
                    log(day, kind="odds", venue=v, race=r, target_before=b, attempt=attempt, ok=False, error="finished_after_deadline")
                    break
                (day / f"odds_{v:02d}_{r:02d}_{t1:%H%M%S}.json").write_text(json.dumps(
                    {"fetched_at": t1.isoformat(timespec="seconds"), "mins_to_deadline": round(mins, 2), "before": before, "odds": odds}, ensure_ascii=False))
                log(day, kind="odds", venue=v, race=r, target_before=b, attempt=attempt, ok=True, mins_to_deadline=round(mins, 2),
                    seconds=round((t1 - t0).total_seconds(), 1), n_trifecta=len(odds.get("3連単") or {}), has_exhibit=bool(before.get("exhibit")))
                break
            except Exception as e:  # noqa: BLE001
                log(day, kind="odds", venue=v, race=r, target_before=b, attempt=attempt, ok=False, error=str(e)[:200])
                time.sleep(3)


if __name__ == "__main__":
    main()
