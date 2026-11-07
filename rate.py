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


def main():
    cfg = load_config()
    try:
        teams, aliases, matches = load_inputs()
    except InputError as e:
        print("入力データにエラーがあります:\n" + str(e), file=sys.stderr)
        sys.exit(1)

    result = compute(cfg, teams, aliases, matches)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    shutil.copy(ROOT / "index.html", SITE / "index.html")

    s = result["stats"]
    print(f"OK: {s['teams']}校 / {s['events']}大会 / {s['matches']}試合 → {OUT.relative_to(ROOT)}")
    for k, t in enumerate([t for t in result["teams"] if t["active"]][:10], start=1):
        print(f"  {k:2d}. {t['name']:<16} {t['rating']:5d} ±{t['rd']}")


if __name__ == "__main__":
    main()
