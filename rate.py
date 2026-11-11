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
                "da": round(to_rd(A["phi"])), "db": round(to_rd(B["phi"])),   # 試合前の ±RD
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
    "og.png": (
        "iVBORw0KGgoAAAANSUhEUgAABLAAAAJ2CAMAAAB4notuAAAAkFBMVEX///////78//36+vP99e329fH09e/28uvz8u3z7+fz7+bz"
        "7+Xv8evy7eTr7ebm6eLlw7nQ4djH2dDH0MqqzcGvw7q2tbKJu6xzsJ6RopqHhoNUoIs8knsjhWwUfWIRfGAPfGAPe2APe18Pe14O"
        "e1/aTDjZQzDZQy/ZQy7ZQS1qamapQzU/PzskJCEGcVUbGxhHjtDLAAA/BklEQVR42u2da3uiSLdAZ95EYhKF4RJQQTyx7e4ZHrX/"
        "/787xf0iICgo6FpfOm0SMQjLXbt27fprCwAwEv7iFAAAwgIAQFgAgLAAABAWAADCAgCEBQCAsAAAEBYAICwAAIQFAICwAABhAQAg"
        "LAAAhAUACAsAAGEBACAsAHgkYU0AAEYCwgIAhAUAgLAAAGEBACAsAACEBQAICwAAYQEAICwAQFgAAAgLAABhAQDCAgBAWAAACAsA"
        "EBYAAMICAEBYAICwAAAQFgAAwgIAhAUAgLAAABAWACAsAACEBQCAsAAAYQEAICwAQFicAgBAWAAACAsAEBYAAMICAEBYAICwAAAQ"
        "FgAAwgIAhAUAgLAAABAWACAsAACEBQCAsAAAYQEAICwAAIQFAAgLAABhAQAgLABAWAAACAsAAGEBAMICAEBYAAAICwAQFgAAwgIA"
        "hAUAgLAAABAWACAsAACEBQCAsAAAYQEAICwAAIQFAAgLAABhAQAgLABAWAAACAsAAGEBAMICAEBYAAAICwAQFgAAwgIAQFgAgLAA"
        "ABAWAADCAgCEBQCAsAAAEBYAICwAAIQFAAgLAABhAQAgLABAWAAACAsAAGEBAMICAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWAADC"
        "AgCEBQCAsAAAEBYAICwAAIQFAICwAABhAQAgLAAAhAUACAsAAGEBACAsAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWAADCAgCEBQCA"
        "sAAAEBYAICwAAIQFAICwAABhAQAgLAAAhAUACAsAAGEBACAsAEBYAAAICwAAYQEAwgIAQFgAAAgLABAWAADCAgBAWACAsAAAEBYA"
        "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"
        "pkVyhbBKVsQVhaWfDybOCiufetIvEJZ5Mst2WE7ux77twhw9OxZUr64/raqhP5H6FUm+vK9WE4QVGGu3+3qJSqTmn36Q1ZWyxOSg"
        "CK/em/iqv4XQNxGWfrGwzBIZVbezqrxqzwqraS6kSlinurrb9GBhnNzYM0lXHXPaQcF86efWqa6uGTTnmyfvZwgrqiDdHhdv0WSh"
        "CLLeO1JWMBqUpMpqhlNjGccfm1EJy1xF7Et80UhYmWroNENxIqzsYn3zMmEpxVyI3EZYJbq6Y/qqEGA1jTz0MHOldLPCp6ygqqSI"
        "fjXtKL469P7xMBphbTebIPWeBFnz65Ulfvu//8z5y+f8ozHCWLvvzZiEpddthNNEWOr+T1vCDK7X9MenxdWE8Yv09KbCmpbp6q7D"
        "wXzA2PhO9vYrtbMliUW0kujqqv5ayyv38XlgYUWJrHjsJqYLleuU9UtUtove7U1Hg9nWDZ0vK+x1SLiv2UKwgbCyvqpX0KHwUdte"
        "WMXc/n7frFtDeWcD776bFmarv5vP9U+7XEOdf2azvP3D5WdJWnVVePqQwhKJrONiFgdZn/O3WFm/Lku1//dLJK/eotFg8yjLXwjt"
        "jklYq5rFHeeFtdxna5hrvZNrPiVdIKxlcei5L//4zwtrapauFb5zeNVtA87rhaWvyhdUr2ZdNab5c4sTPiphBcNC/TXOZAllTQJl"
        "/dvSWeLH/VS7qUykeRKwvX40zmO9dL6ssFdhqTX14w0irMx9Z9b3ONBW+baYbYVlnkj1UJ5gyQhLMys6G3j33hM6O1raS3cV1tSs"
        "WqO4N7sr5VpNEFbJsHC3UOIgK4iyRPpd2Kd5nPUryFyJVPtcRFexrkSF1ddb47Fh9wuh+50l3Fd3lGySw1qmP3NGWEknv7D0qaWw"
        "cjeAmhZinA4L9dJhaFc34qT7vi1Xxx5XCEtZVjerWcndVcrfxFejE5YfZK2N14yyPl4kJXHWryahlW+rf6aT94yuXg3nuHhpLKzO"
        "F0L3KywvVzo9aSus+GvROfmMsOKODlGpZjthfZ3W8+gV96h+Lue/nA7KV9c34LxMWIq5qutVc03Jx8lkoychrMpMlqO9JMoS+pgG"
        "zvr5XyStMm2Fjwey8m31In3GlQxzEaVp9nGXNpBvYKxgIfRuLMIKhHM47MWO8Lo0aS+s8Nn9JTfnhBWO6uLS8lbCKravmqZjRK+m"
        "vulP1eq7Afmqg2R0e2HNVvV9ta6y+slqzT+r2/hqjMLabsS40NbSKEuEWUI6wlmRtGJtZfk3fFzIai69iCZ9qe3eXtXF7uhuxD6I"
        "VvMY603udFnhTQpHJ3WjxfpKdy+a9zsrLP+F7OVJe2Etyzo1rCoGG2bdfXj35NWJfTtYDnxBhHVmRveK0aB2+tSrW53ZMQpLBFmb"
        "44+csoR45m/SJJRWrK0sv37+NP/5R5FeJiJxlZSJhrpyj1s3GGzuvprHWMFC6N2zCEvM0IdRgpqQzhwmhImrdPGzXOjXmRaOFr4x"
        "mZ7eAv484b4iQFkOOrqaSPnXt5fvIizlUOOrK6yueveckB2nsISytqGy0ljJj7OEtF5epHehLZ+fAWbwtSICKyGrz1RW4jc/X198"
        "Xe3cuB1zvOVFo8R7l/urdiesfCOEsxwaLn5WTi7Jqvbm8tf5F2mebbAQZFjMqmV43pB1VSy07aSf3SU5rGqte1esdPb+3HX501iF"
        "FSnLeHv1I6aPrLREgkmavOSYSKJ3TCayCiOy1zfdFk/ibjJ7TWstjCWWFbqDE9alNNk1p5GwmrxI80yrkzNNgStih7gVy51ZHv50"
        "v1rloqS7V2H1iw2qLEvrc2/5KTFeYQXKOjqW+pp3lrBWoK08ua1wxP/fX18VyzmK6GqTy+cnbWwaGWvb1ULoZxZWfp90r0l+pCx2"
        "8Mwh2Eoyu2s7fLWw1EOXVp9VVLzdKt0+fmEFuayja38pobPmzUoS/NjqVTbs9fG4cU8ayLcylv7dkbGeV1iFO9yT1Lqlj1Ux2X4Q"
        "Y8Gy9dcdZXcuK2tYnUxImFK3trp5N4xxC8ufMdyFznqLpDWvd5Uvq1c1sFU+uEpbBcYN5JuUvBsdLSt8WmEV7nCvZEHPYXpmXv3g"
        "6ZOBUFwnfugq7LtMWLm9bISt5M5W4XSwsOc5heU7JnCWszB8ab2+fSTDwIymwtSW+Parolu2K2y1cTcVm/Ss542NJfs7Qn8jrEuF"
        "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"
        "AIxGWM+nLC41gBELa4KvAGA0wnomZXGdAYxeWM+iLK4ygIcQ1hM4i0sM4IGE9djK4gIDeDBhPayzuLoAHlJYjyctriyAhxbWo2iL"
        "awrgaYQFAICwAABhAQAgLAAAhAUACAsAAGEBACAsAEBYAAAICwAQFgAAwgIAQFgAgLAAABAWAADCAgCEBQCAsAAAEBYAICwAAIQF"
        "AICwAABhAQAgLAAAhAUACAsAAGEBACAsAEBYAAAICwAAYQEAwgIAQFgAAAgLABAWAADCAgBAWACAsAAAEBYAICwAAIQFAICwAABh"
        "AQAgLAAAhAUACAsAAGEBACAsAEBYAAAICwAAYQEAwgIAQFgAAAgLABAWAADCAgBAWACAsAAAEBYAAMICAIQFAICwAAAQFgAgLAAA"
        "hAUAgLAAAGEBACAsAEBYAAAICwAAYQEAwgIAQFgAAAgLABAWAADCAgBAWACAsAAAEBYAAMICAIQFAICwAAAQFgAgLAAAhAUAgLAA"
        "AGEBACAsAACEBQAICwAAYQEAICwAQFgAAAgLAABhAQDCAgBAWACAsAAAEBYAAMICAIQFAICwAAAQFgAgLAAAhAUAgLAAAGEBACAs"
        "AACEBQAICwAAYQEAICwAQFgAAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgAAwgIAhAUAgLAAAGEBACAsAACEBQAI"
        "CwAAYQEAICwAQFgAAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgAAwgIAhAUAgLAAABAWACAsAACEBQCAsAAAYQEA"
        "ICwAAIQFAAgLAABhAQDCAgBAWAAACAsAEBYAAMICAEBYAICwAAAQFgAAwgIAhAUAMBD+H84ueBbNZR1nAAAAAElFTkSuQmCC"
    ),
    "icon-180.png": (
        "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"
    ),
    "icon-192.png": (
        "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAMAAABlApw1AAAAwFBMVEX///////79/v78/Pn/+PD1/PP1+vP0+fD/9+/+9e378+v0"
        "9u3z8+r38ejz8ejz7+bw8Ojy7uXz7eTx7uXz7OPy7OPw7uTw7eTv7eTv7ePu7OPt5t3S39XswrbllIary72FuKdhppI+k3sdgmcU"
        "fWITfWITfWESfWESfGERfGAQfGAQe18Pe2APe18Oe1/gbVzbUT7ZRjLZRTHZRDDZQzDZQy/ZQCzYPCjYOSXXNyILeV0IeFsFdlkC"
        "dFcAclUAcVMEefuBAAAL4UlEQVR42u2de3eiSBOHM7MqDQR6ABUVCALKzVfxst5AyPf/Vm81ZDJJJiReaGP2UH9l92Ry6qGr6equ"
        "rh93029udzVADVAD1AA1QA1QA9QANUANUAPUADVADVAD1AA1QA1QA0wnYegXFoaTbwYQ+q4XzOI4LSyOZ4Hn+uH3AAg9b5akWXoY"
        "OY5dmOOMDvB/kpnnhTcOEHp+nGahYw9NU9c0jiXGaZpumkPbCbM09ikyXAzge3GWOpapD9gGYjlBUAoTBI5FDXagm5aTZrHn3yTA"
        "xAtS4v0AIU5Qeqqqdv4Y/FdPETiEBoQhDbzJMaM5BjtlwC4CcGdpYhPvBVVV5E4bizzDNAtjGF7E7Y6sqKpAGOwknbmfeO+GSfoI"
        "libwI30Ab5pNLZ0j3kttWeSbLVEzDOOhMPhJE1tNXpTbEmHgdAt+3/vg743jLHGsIZjlJFk8pgwQ+Cm4j1ilB97ft5gOeB7Nl7vt"
        "vrDtbjmPgKPDtO6BoaewCBBSPyj7e0HmWHrrLreWbjlZENAEGCeprSNOVaS22GI0I5pvwOnNerlcFLZcrjcAs5lHhsa0xLakqBzS"
        "7TR5/8l68cH6Ba7/zA1++GUdYo8aAHlcJsv1FLnNNztGtNjvNitwerV+aSuAWW12+0VkdJp8W1Z6HGu+/2S9xDGJ9z+KEfhBGEwn"
        "8SgBuPC4BqgLT59vaQ/z3W7z1vdXFJvdbv6gtXgYhS4awJN13/Ffv/vn590L+/nPnX4MwTkAbuqYSOhhCB7tYbXfLEu9/82w3OxX"
        "gCC2cU9AppO+IQjiEfh/98aAYBQH1QNMgtTWUB/LUlN72OzXi4+9f2JYrPebB60pybiPNDsNXq0JwcH8239CYB6qBwjCzGLJ4+d5"
        "Yw7ur481QJgbPE8GgbWy8IVn40frPf8JgfU4rhggmB2GSFIkualF+83x7ucIm32kNWX412h4mD0ThLHz6+fP9wB+/vzlxGGlAEGc"
        "mA0Vy7hlLHZHBc/rQNotjBb8a7VhJs/hPc6su3f9B4I7KxtXCeDDbENk9t4/nPr4nwfh4Z7MZQQT9Cm/CxP9RxnADz2pcgSCONDR"
        "AMK/E+2Xq/U5tlruow5MhAHSg2IMgsRplQO0nCSoDCCcpmYD/GeM+W6xPtcWu7nBAEHDTKfk6XppaQTlMZR6lQEEEP993Ibw357v"
        "PxBsYSK0cR/mQZC/g4YfAQw/eQ+dADDKhuT5X+r/M8GgMcxGVwSAlwXXhfi/2P+CAOZBlyPvmGsB+IkzEJQ2r13uf06g8W1FGDiJ"
        "fyWAyTTWWVUSO1EF/hOCqCNKKqvH0/F1JvE4nwAyE+2r8B8I9hEjF9PgKq9R72ALGLebDxX5Twgemm2MBfvgXmUhm+lcV+aNzXpV"
        "EcBqvTF4ucvps2ukEm5mNXoYi9F2ua7KlttIxLjXsLIp9WQO8kVNkKoMoOcgkgTN+V9GO512YQb3ZF6rLoCegkjj5R6Zx5Q3NMUA"
        "wBtoV+UAkKwI3kRkCGaUt5TFADDGbrmu1pY7gymGgOqm/mkA+IgCQMTnQxCPaB6rwCsIkQHYVu0/eRORIUBW5tM92NI5pd2qfgDy"
        "IWi1FU6nerToH2xOknijevdzBIOXJM4++PQOd2EKo17Va8CrtaCHhplL7Xg9nI10AbLQ+XZFAWC1nUNWKuijWUirwAERRNYAY0cp"
        "hMibVBLyGKJTYsoXAVoR9DuGGkUMUQGYwEZGwRKdCMpjSMIKbGwmlAACspMkeTQd/4GAZNVkbxnQAfBTm1U61CKoiKGOwtqpTweA"
        "8hS4fBJ8PgImUulNgadJoCKT0giEM1fn1HttQXEOLLR7ldPdWUgFgGSiHZ5GIvcioeM7JCOlAhAktip2qC1jv5eyjqjaZ76G7r74"
        "JXTxa+i/DvC0m4yohlBU7CtdegA8ZQC+BqgBaoAaoHQdsK60Dlj1QvZ+LkQqM50W5Vyo1SGVmoBeNspQzkYZetloCAffAtkPrKnt"
        "B9ZkPwAnQ3QAyGUqFhLqOcUNDRxtqax5CCjuiWlmcxfmckfsiS3UpX4q0UUWrVMJ2JJhLFOcxXmJAGOb1rnQdEaqA6JGaxLAFNBE"
        "UiGY0Tpa9DITUT8bRWbm0QKASQD1maZBDcBoQo3m7ClwRH0gL/HRiqE8gqTzl7HjSkwm6lKt0HSReXZ54BgASEi7tN5D+Tuoy54f"
        "QUcAPJeJqZRZfxeKQ4pNQEWVr0VjGsMUbj3X+KgBQI1DwhLG88qHYLmdY/jL0tnVjSML3V6aLwXVD0H+DoVFIPWmVAHydEKq9rrT"
        "85Un6YI04oT7QiaicF2luKwCq/D5M+BIgGIIqruy+OLi4qUDcPSdOdgVSPeVLsdkEb6XLikRnwCQrwUVZ0RPWdAla8AJ1y5d2NdA"
        "4wZT6b1R2AeQNM69SjcrqVb2JFGaV3RvDroI4K91WTMbT68CEORBBCX7xaaKV9Fyk99fh6uo1uPoOv3EbmpzClw+NnYVnLCs1juD"
        "XDyGEBrY6fg6DdFu0f/AVEBA/GeI/xh6CC4kOKGDA46ICMHlY/D8/ImpQHAYXwfgqQeLEFwyD5Yv/ce4z+qjxLtKT72fjHQ2J9hu"
        "Luhi2mxf+l8QxP5VRAHGB2fA9S7rA3rqAHpp/eeeMuqqBuODPWChE4uBRsrFORNhBQmQxrz2H+c9ZZPwKrIM3sEpevnks3oRSSei"
        "zL/1H5OesqIrjr6uxPgw0kkzjQjtfPvlaXN5uYSGXEaU8d/Wa8Dx9OQqwhhuHJgNJe/HfdhuT4ij1WK7fSDduPg96zfOXJJP7+j2"
        "48OQE2Ai3DeNaL9dHDcKy8V2HxnN+zYuMUjrzsqr786SNICedCLIwIgQR7vVp8MAv7GD6BEZIs5QAiBh6PMeXUlbZUxUAThoq8dN"
        "yYi2IAvwAcNqAZIA28iQmvD7PQ5hqWQIRM4+g+A8XQk3Ti29gUHUAzd5A4QZ9iArsVz9+8b3f0GTYbXZgyiDwTcxyHvghj5UBKWE"
        "QIC0yL2Suk3gZ6MhRgRBAm0JYAA/t7mux7KwXNtjC2TgPWhKSMR9hIejR4stGwOS2B3ca8nzuLPMNjHIS4C6h8i0QFolmi/A4f2u"
        "MPhhuwB1FaPTYkSi6iEgbNrZbASiCFIJQQ82mIl7LX2hiZtkRJuH66qSTMRtGKwRbZuoMKJvo2GGyNvIktrliEJPlrgTIsKAymZy"
        "j4PEzr+aQFLgJqkz1FkkdMk4yPieaTWbfGHNZou5xzJ59l0BsfrQSRM3KGo+Q2gMLyFAunNaWnSZwlMA+lQeyFOxDQ6rvULfSS6s"
        "0HjqqZhrsCBT5YFKVfBbyu1glhLkrfbBFTW2QlB5gu6doY4RkanCitIrTFEwkadCWIeWmAzUnf6kOuEkFxd43/rotLTocpWzieeB"
        "yNnUtoagcSawoAxGDLGsADpnQ8uegsyZ91pf60ne4X1TT0uLKtGZm/julMjM+URkblgYkZrzidDc1PUn7wpslEfR8HF8faU/Ius3"
        "iw9pmhWWpod4RsT/3k/Li81dBWnRXbVKi4Hvu4X5fvCR2mK+uVPL06KjTyq+TO2SbO5A5qGEAB2dXH+dXOc4tQcfpkXjW9cbhbQI"
        "fZgWjW9dMBUUJUoTOzjvco4i+FLFV2j05ZSypOLI866vlawdPQ4/WJKPOu/6WoBJkCtelSxoR513fbFoMNFcQh8ldp+ed3216nEw"
        "/Sixg7ToM+3hL5dthtJPeVrUhxpmcOu60/mZdxmBCtcZg1sXzvY+IFCUxie90reg/O2WpkUSaAL608nNS5eXpUUKOa2bTb6B9voo"
        "szlR+Tulg6TUm3wL8XhyWvQ2LVKF7hEbm1tRv/87LepDQnpEGf9m5PshuX511tJnYUsw+kbfH5iQMnTvZSp33Jbmdj6gkEtR9v8k"
        "cqPb39C8Jfhz3tVvwGZg/O0+YZGfd/XzBMI8unh/U9/gIOddkFSQouvRpfvb+ohInhbl24Dgm34FhZwWIetwwvn0rX3GZZzZ1mMQ"
        "fuPv0PjJaReRb+9DOoFffwmoBqgBaoAaoAaoAWqAGqAGqAFqgBqgBqgBaoAaoAaoAWqAGuC/DvB/lKCn1rmyweIAAAAASUVORK5C"
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


def add_share_preview(cfg, result):
    """LINE などでリンクを送ったときのプレビュー（タイトル・説明・画像）を公開ページに入れる"""
    page = SITE / "index.html"
    if not page.exists():
        return
    import html as _h, os
    title = cfg.get("site_title") or "卓球 団体戦レーティング"
    st = result["stats"]
    top = [t for t in result["teams"] if t["ranked"]][:3]
    desc = f"大阪の高校卓球・学校対抗（団体戦）のレーティング。{st['teams']}校・{st['matches']}試合の結果から計算しています。"
    if top:
        desc += "現在の上位：" + "、".join(f"{k}位 {t['name']}（{t['rating']}）" for k, t in enumerate(top, 1))
    repo = os.environ.get("GITHUB_REPOSITORY", "")   # GitHub Actions で「持ち主/リポジトリ名」が入る
    base = f"https://{repo.split('/')[0].lower()}.github.io/{repo.split('/')[1]}/" if "/" in repo else ""
    tags = [("og:type", "website"), ("og:title", title), ("og:description", desc), ("og:site_name", title)]
    if base:
        tags += [("og:url", base), ("og:image", base + "og.png"), ("og:image:width", "1200"), ("og:image:height", "630")]
    meta = "\n".join(f'<meta property="{k}" content="{_h.escape(v)}">' for k, v in tags)
    meta += f'\n<meta name="description" content="{_h.escape(desc)}">\n<meta name="twitter:card" content="summary_large_image">'
    text = page.read_text(encoding="utf-8")
    page.write_text(text.replace("</head>", meta + "\n</head>", 1), encoding="utf-8")


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
    add_share_preview(cfg, result)
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
