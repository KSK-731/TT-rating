#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
卓球 団体戦レーティング計算スクリプト（Glicko-2）

同じフォルダの CSV を読み込み、公開用フォルダ _site/ を作ります。
外部ライブラリは不要です（Python 3.9 以降の標準ライブラリのみ）。

    python scripts/rate.py

入力に誤りがあると、行番号つきでエラーを表示して終了コード 1 で止まります。
（GitHub Actions では赤い×印になり、サイトは前回の状態のまま残ります）
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
import shutil
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT
SITE = ROOT / "_site"
OUT = SITE / "data" / "ratings.json"

SCALE = 173.7178  # Glicko-2 内部スケールへの変換係数

DEFAULT_CONFIG = {
    "site_title": "卓球 団体戦レーティング",
    "highlight_team": "",
    "initial_rating": 1500,
    "initial_rd": 350,
    "initial_volatility": 0.06,
    "tau": 0.5,
    "max_rd": 350,
    "season_start_month": 4,
    "season_rd_increase": 50,
    "use_score_margin": False,
    "active_years": 3,
    "peak_rd_limit": 150,
    "rank_rd_limit": 150,
}


# ---------------------------------------------------------------- 入力 ----

class InputError(Exception):
    pass


def norm(s: str | None) -> str:
    """全角英数字・全角スペースを半角に揃え、前後の空白を除く。"""
    if s is None:
        return ""
    return unicodedata.normalize("NFKC", s).strip()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = ROOT / "config.json"
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


def fix_separators(text: str) -> str:
    """区切りに「、」「，」を使った行も読めるようにする。
    1行目（見出し）の列数に足りない分だけ、左から順に「、」を「,」に置き換える。
    余った「、」は最後の列（備考）の文字としてそのまま残る。"""
    lines = text.replace("，", ",").splitlines()
    if not lines:
        return text
    lines[0] = lines[0].replace("、", ",")
    need_total = lines[0].count(",")
    out = [lines[0]]
    for line in lines[1:]:
        need = need_total - line.count(",")
        while need > 0 and "、" in line:
            line = line.replace("、", ",", 1)
            need -= 1
        out.append(line)
    return "\n".join(out) + "\n"


def read_csv(path: Path, required: list[str]) -> list[tuple[int, dict]]:
    """UTF-8 / Shift_JIS（Excel 保存）の両方を受け付ける。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise InputError(f"{path.name}: 文字コードを判別できません。UTF-8 で保存してください。")

    text = fix_separators(text)
    reader = csv.DictReader(io.StringIO(text))
    header = [norm(h) for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [c for c in required if c not in header]
    if missing:
        raise InputError(f"{path.name}: 1行目に列 {missing} がありません（現在の列: {header}）")

    rows = []
    for i, row in enumerate(reader, start=2):
        row = {k: norm(v) for k, v in row.items() if k}
        if not any(row.values()):
            continue  # 空行は無視
        rows.append((i, row))
    return rows


DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")


def parse_date(s: str) -> dt.date:
    m = DATE_RE.match(s)
    if not m:
        raise ValueError
    return dt.date(int(m[1]), int(m[2]), int(m[3]))


def load_inputs():
    errors: list[str] = []

    teams_rows = read_csv(DATA / "teams.csv", ["学校名"])
    teams: dict[str, str] = {}
    for line, r in teams_rows:
        name = r["学校名"]
        if not name:
            continue
        if name in teams:
            errors.append(f"teams.csv {line}行目: 「{name}」が重複しています")
        teams[name] = r.get("備考", "")

    aliases: dict[str, str] = {}
    alias_path = DATA / "aliases.csv"
    if alias_path.exists():
        for line, r in read_csv(alias_path, ["旧名称", "現名称"]):
            old, new = r["旧名称"], r["現名称"]
            if not old or not new:
                continue
            if new not in teams:
                errors.append(f"aliases.csv {line}行目: 現名称「{new}」が teams.csv にありません")
            aliases[old] = new

    def resolve(name: str) -> str:
        return aliases.get(name, name)

    matches = []
    seen: dict[tuple, int] = {}
    for line, r in read_csv(DATA / "matches.csv",
                            ["日付", "大会名", "チームA", "チームB", "得点A", "得点B"]):
        where = f"matches.csv {line}行目"
        try:
            date = parse_date(r["日付"])
        except ValueError:
            errors.append(f"{where}: 日付「{r['日付']}」を読めません（例: 2026-11-03）")
            continue
        event = r["大会名"]
        if not event:
            errors.append(f"{where}: 大会名が空です")
        a, b = resolve(r["チームA"]), resolve(r["チームB"])
        bad = False
        for side, orig in (("チームA", r["チームA"]), ("チームB", r["チームB"])):
            if resolve(orig) not in teams:
                errors.append(f"{where}: {side}「{orig}」が teams.csv にありません"
                              "（表記ゆれか、新しい学校なら teams.csv に追加）")
                bad = True
        if a == b and not bad:
            errors.append(f"{where}: 同じ学校同士の試合になっています（{a}）")
            bad = True
        try:
            sa, sb = int(r["得点A"]), int(r["得点B"])
            if sa < 0 or sb < 0:
                raise ValueError
        except ValueError:
            errors.append(f"{where}: 得点「{r['得点A']}-{r['得点B']}」が数字ではありません")
            bad = True
        if bad:
            continue
        note = r.get("備考", "")
        key = (date, event, tuple(sorted([(a, sa), (b, sb)])), note)
        if key in seen:
            errors.append(f"{where}: {seen[key]}行目とまったく同じ試合です（{date} {event} {a} {sa}-{sb} {b}）"
                          "。二重に入力していないか確認してください")
            continue
        seen[key] = line
        matches.append({"line": line, "date": date, "event": event,
                        "a": a, "b": b, "sa": sa, "sb": sb, "note": note})

    if errors:
        raise InputError("\n".join(errors))
    return teams, aliases, matches


# ------------------------------------------------------------ Glicko-2 ----

def g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def expect(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-g(phi_j) * (mu - mu_j)))


def new_volatility(phi, sigma, v, delta, tau, eps=1e-6):
    a = math.log(sigma * sigma)

    def f(x):
        ex = math.exp(x)
        return (ex * (delta * delta - phi * phi - v - ex)
                / (2.0 * (phi * phi + v + ex) ** 2)) - (x - a) / (tau * tau)

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        B = a - k * tau
    fA, fB = f(A), f(B)
    while abs(B - A) > eps:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0:
            A, fA = B, fB
        else:
            fA /= 2.0
        B, fB = C, fC
    return math.exp(A / 2.0)


def glicko2_update(st: dict, games: list[tuple[float, float, float]], tau: float, max_phi: float):
    """games: [(相手の mu, 相手の phi, 結果 s)]。st を更新する。"""
    mu, phi, sigma = st["mu"], st["phi"], st["sigma"]
    if not games:
        st["phi"] = min(math.sqrt(phi * phi + sigma * sigma), max_phi)
        return
    inv_v = 0.0
    total = 0.0
    for mu_j, phi_j, s in games:
        gj = g(phi_j)
        e = expect(mu, mu_j, phi_j)
        inv_v += gj * gj * e * (1.0 - e)
        total += gj * (s - e)
    v = 1.0 / inv_v
    delta = v * total
    sigma2 = new_volatility(phi, sigma, v, delta, tau)
    phi_star = math.sqrt(phi * phi + sigma2 * sigma2)
    phi2 = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    st["mu"] = mu + phi2 * phi2 * total
    st["phi"] = min(phi2, max_phi)
    st["sigma"] = sigma2


def to_rating(mu): return mu * SCALE + 1500
def to_rd(phi): return phi * SCALE


# ------------------------------------------------------------ 計算本体 ----

def season_of(d: dt.date, start_month: int) -> int:
    return d.year if d.month >= start_month else d.year - 1


def outcome(sa: int, sb: int, use_margin: bool) -> float:
    if sa == sb:
        return 0.5
    if use_margin and sa + sb > 0:
        return sa / (sa + sb)
    return 1.0 if sa > sb else 0.0


def compute(cfg, teams, aliases, matches):
    tau = cfg["tau"]
    max_phi = cfg["max_rd"] / SCALE
    season_bump = cfg["season_rd_increase"] / SCALE
    use_margin = cfg["use_score_margin"]

    # 日付順（同じ日付はファイル内の順）に並べ、日付＋大会名ごとに 1 期間とする
    matches.sort(key=lambda m: (m["date"], m["line"]))
    periods: dict[tuple, list] = {}
    for m in matches:
        periods.setdefault((m["date"], m["event"]), []).append(m)

    state: dict[str, dict] = {}
    info: dict[str, dict] = {}
    out_matches = []
    prev_season = None

    for (date, event), ms in periods.items():
        season = season_of(date, cfg["season_start_month"])
        # 年度が変わったら全校の RD を広げる（部員の入れ替わりを反映）
        if prev_season is not None and season > prev_season:
            for st in state.values():
                for _ in range(season - prev_season):
                    st["phi"] = min(math.sqrt(st["phi"] ** 2 + season_bump ** 2), max_phi)
        prev_season = season

        for m in ms:
            for t in (m["a"], m["b"]):
                if t not in state:
                    state[t] = {"mu": (cfg["initial_rating"] - 1500) / SCALE,
                                "phi": cfg["initial_rd"] / SCALE,
                                "sigma": cfg["initial_volatility"]}
                    info[t] = {"wins": 0, "losses": 0, "draws": 0, "history": [],
                               "first": date, "last": date, "peak": None}

        games: dict[str, list] = {t: [] for t in state}
        for m in ms:
            A, B = state[m["a"]], state[m["b"]]
            s = outcome(m["sa"], m["sb"], use_margin)
            games[m["a"]].append((B["mu"], B["phi"], s))
            games[m["b"]].append((A["mu"], A["phi"], 1.0 - s))
            p = 1.0 / (1.0 + math.exp(-g(math.hypot(A["phi"], B["phi"])) * (A["mu"] - B["mu"])))
            for t, won, lost in ((m["a"], m["sa"] > m["sb"], m["sa"] < m["sb"]),
                                 (m["b"], m["sb"] > m["sa"], m["sb"] < m["sa"])):
                key = "wins" if won else "losses" if lost else "draws"
                info[t][key] += 1
                info[t]["last"] = date
            out_matches.append({
                "date": date.isoformat(), "event": event,
                "a": m["a"], "b": m["b"], "sa": m["sa"], "sb": m["sb"],
                "ra": round(to_rating(A["mu"])), "rb": round(to_rating(B["mu"])),
                "pa": round(p, 3), "note": m["note"],
            })

        before = {t: to_rating(state[t]["mu"]) for t in state}
        for t, st in state.items():
            glicko2_update(st, games[t], tau, max_phi)

        played = {m["a"] for m in ms} | {m["b"] for m in ms}
        for t in played:
            r, rd = to_rating(state[t]["mu"]), to_rd(state[t]["phi"])
            info[t]["history"].append([date.isoformat(), event, round(r), round(rd),
                                       round(r - before[t])])
            if rd <= cfg["peak_rd_limit"] and (info[t]["peak"] is None or r > info[t]["peak"][0]):
                info[t]["peak"] = [round(r), date.isoformat()]

    latest = matches[-1]["date"] if matches else dt.date.today()
    cutoff = latest - dt.timedelta(days=round(365.25 * cfg["active_years"]))

    team_list = []
    for t, st in state.items():
        i = info[t]
        team_list.append({
            "name": t,
            "rating": round(to_rating(st["mu"])),
            "rd": round(to_rd(st["phi"])),
            "volatility": round(st["sigma"], 4),
            "wins": i["wins"], "losses": i["losses"], "draws": i["draws"],
            "first": i["first"].isoformat(), "last": i["last"].isoformat(),
            "active": i["last"] >= cutoff,
            "ranked": i["last"] >= cutoff and to_rd(st["phi"]) <= cfg["rank_rd_limit"],
            "peak": i["peak"],
            "note": teams.get(t, ""),
            "aliases": sorted(o for o, n in aliases.items() if n == t),
            "history": i["history"],
        })
    team_list.sort(key=lambda x: -x["rating"])

    return {
        "title": cfg["site_title"],
        "highlight": cfg["highlight_team"],
        "generated": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M"),
        "latest": latest.isoformat(),
        "active_years": cfg["active_years"],
        "rank_rd_limit": cfg["rank_rd_limit"],
        "method": {k: cfg[k] for k in ("initial_rating", "initial_rd", "tau",
                                       "season_rd_increase", "use_score_margin",
                                       "season_start_month")},
        "stats": {"matches": len(matches), "events": len(periods), "teams": len(state),
                  "first": matches[0]["date"].isoformat() if matches else None},
        "teams": team_list,
        "matches": out_matches,
    }


def rank_map(result: dict) -> dict:
    """順位が付く学校の {学校名: 順位}。"""
    return {t["name"]: k for k, t in enumerate([t for t in result["teams"] if t["ranked"]], start=1)}


def add_history_views(cfg, teams, aliases, matches, result):
    """前回大会からの順位変動と、年度末ごとのランキングを付け加える。"""
    matches = sorted(matches, key=lambda m: (m["date"], m["line"]))
    if not matches:
        result["prev"] = None
        result["seasons"] = []
        return
    # 1) 最新の大会（日付＋大会名）の直前の順位
    last = (matches[-1]["date"], matches[-1]["event"])
    before = [m for m in matches if (m["date"], m["event"]) != last]
    result["prev"] = None
    if before:
        prev = compute(cfg, teams, aliases, [dict(m) for m in before])
        result["prev"] = {"event": f"{last[0].isoformat()} {last[1]}", "ranks": rank_map(prev),
                          "ratings": {t["name"]: t["rating"] for t in prev["teams"]}}

    # 2) 年度末（次の年度が始まる直前）時点のランキング
    sm = cfg["season_start_month"]
    seasons = sorted({season_of(m["date"], sm) for m in matches})
    out = []
    for sy in seasons[:-1]:                       # 今の年度は「現在」として表示する
        end = dt.date(sy + 1, sm, 1)
        part = [m for m in matches if m["date"] < end]
        res = compute(cfg, teams, aliases, [dict(m) for m in part])
        rec: dict[str, list] = {}
        for m in part:
            if season_of(m["date"], sm) != sy:
                continue
            for me, my, op in ((m["a"], m["sa"], m["sb"]), (m["b"], m["sb"], m["sa"])):
                r = rec.setdefault(me, [0, 0])
                if my > op: r[0] += 1
                elif my < op: r[1] += 1
        out.append({
            "season": sy, "asof": (end - dt.timedelta(days=1)).isoformat(),
            "teams": [{"name": t["name"], "rating": t["rating"], "rd": t["rd"], "ranked": t["ranked"],
                       "active": t["active"], "season_wl": rec.get(t["name"], [0, 0])}
                      for t in res["teams"] if t["active"]],
        })
    result["seasons"] = out[::-1]               # 新しい年度から


# ---------------------------------------------------------- アイコン ----
# ホーム画面に追加したときのアイコン（画像ファイルを別にアップロードしなくて済むよう、ここに入れてある）
ICONS = {
    "icon-180.png": (
        "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"
    ),
    "icon-192.png": (
        "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"
        "YII="
    ),
    "icon-512.png": (
        "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"
        "OwW5ju4AAAAASUVORK5CYII="
    ),
}
MANIFEST = {"name": "卓球 団体戦レーティング", "short_name": "卓球レート", "start_url": "./", "scope": "./", "display": "standalone", "background_color": "#f6f5f1", "theme_color": "#0f7b5f", "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"}, {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}


def write_icons():
    import base64 as _b64
    for name, data in ICONS.items():
        (SITE / name).write_bytes(_b64.b64decode("".join(data)))
    (SITE / "manifest.webmanifest").write_text(json.dumps(MANIFEST, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    cfg = load_config()
    try:
        teams, aliases, matches = load_inputs()
    except InputError as e:
        print("入力データにエラーがあります:\n" + str(e), file=sys.stderr)
        sys.exit(1)

    result = compute(cfg, teams, aliases, [dict(m) for m in matches])
    add_history_views(cfg, teams, aliases, matches, result)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    write_icons()
    # ページはファイル名を問わずすべて公開する（管理者用ページの名前は自由に変えられる）
    pages = sorted({p.name for p in ROOT.glob("*.html")} | {"bracket.js"})
    for name in pages:
        if (ROOT / name).exists():
            shutil.copy(ROOT / name, SITE / name)
    # 管理者用ページで保存したお知らせと次の大会（壊れていたら公開しない）
    for name in ("notice.json", "next.json"):
        src = ROOT / name
        if src.exists():
            try:
                json.loads(src.read_text(encoding="utf-8"))
                shutil.copy(src, OUT.parent / name)
            except (ValueError, UnicodeDecodeError):
                print(f"注意: {name} が読めないため公開しませんでした", file=sys.stderr)

    s = result["stats"]
    print(f"OK: {s['teams']}校 / {s['events']}大会 / {s['matches']}試合 → {OUT.relative_to(ROOT)}")
    try:
        for k, t in enumerate([t for t in result["teams"] if t["ranked"]][:10], start=1):
            print(f"  {k:2d}. {t['name']:<16} {t['rating']:5d} ±{t['rd']}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
