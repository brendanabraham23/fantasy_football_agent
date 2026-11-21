"""Narrative backtest: do the expert calls inside news articles predict when a player beats his projection?

For each archived player-week (the newest run before that player's kickoff) this fetches the bodies of the
articles that run saw, keeps the paragraphs about the player, has the LLM read off each article's call on him
for that week, and tests those calls against the run's prediction with sentiment taken out. Headlines can't
carry most of these calls ("Start/Sit: A, B, C"), which is why headline sentiment can't be used to judge them.
Writes reports/narratives/: a markdown report, per-row and per-call CSVs. Article text is never stored, only
short quotes.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy import stats as sstats

from . import http, llm, nflverse, sleeper

ET = ZoneInfo("America/New_York")
GN_ID = re.compile(r"news\.google\.com/(?:rss/)?articles/([^?/#]+)")
GN_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

SYSTEM = """You read excerpts from fantasy football articles published before a player's game and report each
article's call on THAT player for THAT week. For every article id return:
kind: "call"    - an explicit recommendation or ranking judgment for this week: start/sit, must-start, fade, sleeper,
                  bust, upgrade/downgrade, smash spot, avoid, ranked higher/lower than usual, flex-worthy or not
      "outlook" - forward-looking information without a clear call: role or snap trends, injury/practice news,
                  depth chart, matchup notes
      "recap"   - mostly about what already happened
      "none"    - no real information about him (passing mention, a list entry with no comment, other player)
lean: -1.0 .. +1.0, the writer's view RELATIVE TO WHAT MANAGERS WOULD NORMALLY EXPECT FROM THIS PLAYER, since his
      usual level is already priced in. "Start him, he's an every-week WR1" ~ +0.1; "surprise must-start / smash
      spot / big upgrade" ~ +0.7; "risky flex, temper expectations" ~ -0.3; "sit him / fade / bust" ~ -0.6;
      "should be benched in all formats" ~ -0.9. Use 0 for recap/none.
week: the NFL week the call is about, or null if unclear.
quote: up to 20 words copied from the excerpt that best support the lean (empty for none)."""
SCHEMA = {"type": "object", "properties": {"articles": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "integer"}, "kind": {"type": "string", "enum": ["call", "outlook", "recap", "none"]},
    "lean": {"type": "number"}, "week": {"type": ["integer", "null"]}, "quote": {"type": "string"}},
    "required": ["id", "kind", "lean", "week", "quote"]}}}, "required": ["articles"]}


@dataclass
class Row:
    week: int
    player_id: str
    name: str
    position: str
    team: str
    group: str | None
    run: str
    generated_at: datetime
    kickoff: datetime
    pre: float                 # the run's prediction with the sentiment multiplier divided out
    sentiment: float           # headline sentiment the run used
    articles: list[dict] = field(default_factory=list)
    actual: float | None = None


def _ts(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def kickoffs(season: int) -> dict[tuple[int, str], datetime]:
    df = nflverse.schedule(season)
    df = df[(df.season == season) & (df.game_type == "REG")]
    out = {}
    for r in df.itertuples():
        t = datetime.strptime(f"{r.gameday} {r.gametime}", "%Y-%m-%d %H:%M").replace(tzinfo=ET).astimezone(timezone.utc)
        out[(int(r.week), r.home_team)] = out[(int(r.week), r.away_team)] = t
    return out


def collect_rows(archive: Path, season: int, weeks: list[int], kick: dict, skip: list[str]) -> list[Row]:
    """Newest archived run before each player's kickoff, keeping only articles published before kickoff."""
    skip_re = re.compile("|".join(skip), re.I) if skip else None
    best: dict[tuple[int, str], tuple] = {}
    for d in sorted(Path(archive).iterdir()):
        m = re.search(r"week(\d+)_(\d{4})$", d.name)
        if not m or int(m.group(2)) != season or int(m.group(1)) not in weeks or not (d / "run.json").exists():
            continue
        w, s = int(m.group(1)), json.loads((d / "run.json").read_text())
        gen = _ts(s["meta"]["generated_at"])
        for pid, p in s["players"].items():
            ko = kick.get((w, p.get("team")))
            if p.get("on_bye") or not p.get("articles") or not ko or gen >= ko:
                continue
            if (w, pid) not in best or best[(w, pid)][0] < gen:
                best[(w, pid)] = (gen, p, d.name, ko)
    rows = []
    for (w, pid), (gen, p, run, ko) in sorted(best.items()):
        arts = [a for a in p["articles"] if (not a.get("published") or _ts(a["published"]) < ko)
                and not (skip_re and skip_re.search(a.get("title", "")))]
        ms = p.get("mult_sentiment") or 1.0
        rows.append(Row(w, pid, p["name"], p["position"], p["team"], p.get("group"), run, gen, ko,
                        round(p["adj"] / ms, 3), p.get("sentiment") or 0.0, arts))
    return rows


# -- fetching ---------------------------------------------------------------------

def resolve(link: str, ttl: int) -> str:
    """Publisher URL behind a Google News RSS link (other links are returned unchanged)."""
    m = GN_ID.search(link or "")
    if not m:
        return link
    gid, html = m.group(1), ""
    for base in ("https://news.google.com/articles/", "https://news.google.com/rss/articles/"):
        html = http.get_text(base + gid, ttl=ttl)
        sg, ts = re.search(r'data-n-a-sg="([^"]+)"', html), re.search(r'data-n-a-ts="([^"]+)"', html)
        if sg and ts:
            break
    else:
        raise ValueError("no Google News signature on the article page")
    req = ["Fbv4je", '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
                     f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{gid}",{ts.group(1)},"{sg.group(1)}"]']
    raw = http.post(GN_BATCH, {"f.req": json.dumps([[req]])}, ttl=ttl,
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}).decode("utf-8", "replace")
    try:
        return json.loads(json.loads(raw.split("\n\n")[1])[:-2][0][2])[1]
    except (IndexError, TypeError, ValueError):
        m = re.search(r'garturlres\\",\\"(https?://[^\\"]+)', raw)
        if not m:
            raise ValueError("couldn't read the URL from Google News' response")
        return m.group(1)


_META = {
    "modified": [r'"dateModified"\s*:\s*"([^"]+)"', r'article:modified_time"\s+content="([^"]+)"',
                 r'og:updated_time"\s+content="([^"]+)"', r'content="([^"]+)"\s+property="article:modified_time"'],
    "published": [r'"datePublished"\s*:\s*"([^"]+)"', r'article:published_time"\s+content="([^"]+)"',
                  r'content="([^"]+)"\s+property="article:published_time"'],
}


def page_dates(html: str) -> dict[str, datetime | None]:
    """Latest modified and earliest published time declared in the page's metadata."""
    out = {}
    for k, pats in _META.items():
        found = [d for p in pats for s in re.findall(p, html) if (d := _ts(s))]
        out[k] = (max if k == "modified" else min)(found) if found else None
    return out


def page_text(html: str) -> str:
    import trafilatura  # only this backtest needs it
    return trafilatura.extract(html, include_comments=False, include_tables=True, favor_recall=True) or ""


def excerpt(text: str, name: str, max_chars: int) -> str:
    """Paragraphs naming the player, each with the one after it (start/sit pieces put the call under a name heading)."""
    last = re.sub(r"\b(jr|sr|ii|iii|iv|v)\.?$", "", name.strip(), flags=re.I).split()[-1].lower()
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    keep: list[int] = []
    for i, p in enumerate(paras):
        if last in p.lower():
            keep += [j for j in (i, i + 1) if j < len(paras) and j not in keep]
    out = "\n".join(paras[i] for i in keep)
    return out[:max_chars]


def fetch(link: str, ncfg: dict) -> dict:
    """{url, text, modified, published} or {url, error}."""
    ttl = int(ncfg.get("cache_days", 30) * 86400)
    url = link
    for attempt in range(3):
        try:
            url = resolve(link, ttl)
            html = http.get_text(url, ttl=ttl, timeout=25)
            return {"url": url, "text": page_text(html), **page_dates(html)}
        except Exception as exc:
            if "429" in str(exc) and attempt < 2:
                time.sleep(10 * 3 ** attempt)
                continue
            return {"url": url, "error": f"{type(exc).__name__}: {exc}"[:160]}
        finally:
            time.sleep(ncfg.get("delay", 0.5))


# -- LLM calls --------------------------------------------------------------------

def read_calls(row: Row, items: list[dict], ncfg: dict, cache: dict) -> list[dict] | None:
    """One LLM call per player-week over all of that week's excerpts; cached by the excerpts' content."""
    if not items:
        return []
    blob = "\n\n".join(f"[{i}] {it['title']}\n{it['excerpt']}" for i, it in enumerate(items))
    key = f"{row.week}|{row.player_id}|" + hashlib.sha1(blob.encode()).hexdigest()
    if key in cache:
        return cache[key]
    prompt = (f"Player: {row.name} ({row.position}, {row.team}). Upcoming game: week {row.week}, kickoff "
              f"{row.kickoff:%Y-%m-%d}.\nReturn all {len(items)} article ids.\n\n{blob}")
    lcfg = ncfg.get("llm", {})
    for _ in range(1 + lcfg.get("retries", 2)):
        try:
            out = llm._call(lcfg.get("command", "claude"), lcfg.get("model", "sonnet"), prompt,
                            lcfg.get("timeout", 300), SYSTEM, SCHEMA)
            got = {int(a["id"]): a for a in out["articles"]}
            if all(i in got for i in range(len(items))):
                res = [{"kind": got[i]["kind"], "lean": max(-1.0, min(1.0, float(got[i]["lean"]))),
                        "week": got[i].get("week"), "quote": str(got[i].get("quote", ""))[:200]} for i in range(len(items))]
                cache[key] = res
                return res
        except Exception as exc:
            print(f"[warn] LLM read failed for {row.name} wk {row.week}: {type(exc).__name__}: {exc}"[:200])
    return None


# -- evaluation -------------------------------------------------------------------

def fit_w(pre, res, s) -> float:
    x = pre * s
    d = float((x * x).sum())
    return float((x * res).sum() / d) if d else 0.0


def evaluate(pre, act, s, week, player, grid, n_boot=4000, seed=0) -> dict:
    """How well `s` explains act - pre under adj = pre * (1 + w*s): fit w, player-bootstrap CI, leave-one-week-out."""
    pre, act, s, week, player = map(np.asarray, (pre, act, s, week, player))
    res, n = act - pre, len(pre)
    out = {"n": n, "players": len(set(player.tolist())), "base_mae": float(np.abs(res).mean())}
    if n < 5 or not np.any(s):
        return out
    ratio = res / pre
    out["spearman"], out["spearman_p"] = map(float, sstats.spearmanr(s, ratio))
    out["w"] = fit_w(pre, res, s)
    rng, ids = np.random.default_rng(seed), np.unique(player)
    by = {p: np.where(player == p)[0] for p in ids}
    boots = np.array([fit_w(pre[i], res[i], s[i]) for i in
                      (np.concatenate([by[p] for p in rng.choice(ids, len(ids))]) for _ in range(n_boot))])
    out["ci"] = [float(v) for v in np.percentile(boots, [2.5, 97.5])]
    out["p_pos"] = float((boots > 0).mean())
    cv = pre.astype(float).copy()
    for wk in np.unique(week):
        tr, te = week != wk, week == wk
        if tr.any():
            cv[te] = pre[te] * (1 + fit_w(pre[tr], res[tr], s[tr]) * s[te])
    out["cv_mae"] = float(np.abs(act - cv).mean())
    out["mae"] = {g: float(np.abs(act - pre * (1 + g * s)).mean()) for g in grid}
    pos, neg = s > 0.15, s < -0.15
    out["beat_pos"] = (float((act[pos] > pre[pos]).mean()), int(pos.sum())) if pos.any() else None
    out["beat_neg"] = (float((act[neg] > pre[neg]).mean()), int(neg.sum())) if neg.any() else None
    out["beat_all"] = float((act > pre).mean())
    out["ratio_pos"] = float(ratio[pos].mean()) if pos.any() else None
    out["ratio_neg"] = float(ratio[neg].mean()) if neg.any() else None
    return out


def actuals(rows: list[Row], season: int, scoring: dict, rec: float, league_id: str | None) -> None:
    """League-scored points; players whose team played but who have no stat line scored 0."""
    for wk in sorted({r.week for r in rows}):
        st = sleeper.stats(season, wk)
        pp = {}
        if league_id:
            try:
                for m in sleeper.matchups(league_id, wk):
                    pp.update({k: float(v) for k, v in (m.get("players_points") or {}).items()})
            except Exception as exc:
                print(f"[warn] matchups for week {wk} unavailable ({exc}); using stat lines only")
        for r in (r for r in rows if r.week == wk):
            line = st.get(r.player_id)
            if r.player_id in pp:
                r.actual = round(pp[r.player_id], 2)
            elif line and (line.get("gp", 1) or 0) > 0:
                r.actual = sleeper.fantasy_points(line, scoring, rec, r.position)
            else:
                r.actual = 0.0


# -- report -----------------------------------------------------------------------

def _f(v, d=3, sign=True):
    return "–" if v is None else (f"{v:+.{d}f}" if sign else f"{v:.{d}f}")


def render(ev: dict[str, dict], funnel: dict, label: str, grid: list[float], ncfg: dict) -> str:
    L = [f"# Narrative backtest: {label}", "",
         "Does an article's call on a player (read from the article body) predict whether he beats the run's "
         "prediction with sentiment removed? Model: `adj = pre x (1 + w x lean)`; positive `w` means the calls help.", "",
         "## Coverage", "", "| step | count |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in funnel.items()]
    L += ["", "## Results", "",
          "| signal | rows | players | Spearman (p) | best w (95% CI) | P(w>0) | MAE w=0 | CV MAE | beat pre: lean>+.15 / lean<-.15 / all |",
          "|---|---|---|---|---|---|---|---|---|"]
    for name, e in ev.items():
        if "w" not in e:
            L.append(f"| {name} | {e['n']} | {e['players']} | too few | | | {_f(e['base_mae'], 2, False)} | | |")
            continue
        bp = f"{e['beat_pos'][0]:.0%} (n={e['beat_pos'][1]})" if e["beat_pos"] else "–"
        bn = f"{e['beat_neg'][0]:.0%} (n={e['beat_neg'][1]})" if e["beat_neg"] else "–"
        L.append(f"| {name} | {e['n']} | {e['players']} | {_f(e['spearman'])} ({e['spearman_p']:.2f}) | "
                 f"{_f(e['w'], 2)} [{_f(e['ci'][0], 2)}, {_f(e['ci'][1], 2)}] | {e['p_pos']:.2f} | "
                 f"{_f(e['base_mae'], 3, False)} | {_f(e['cv_mae'], 3, False)} | {bp} / {bn} / {e['beat_all']:.0%} |")
    L += ["", "MAE by weight:", "", "| signal | " + " | ".join(f"w={g}" for g in grid) + " |",
          "|---|" + "---|" * len(grid)]
    L += [f"| {name} | " + " | ".join(_f(e["mae"][g], 3, False) for g in grid) + " |" for name, e in ev.items() if "mae" in e]
    L += ["", "Notes:",
          "- `narrative` = mean lean of `call` articles about this week; `narrative+outlook` also counts `outlook`; "
          "`headline sentiment` is what the run used, on the same rows.",
          "- Articles whose page says it was modified after kickoff are dropped (pages that don't declare a modified "
          "time can't be checked). Player hub pages are skipped by title "
          f"(`narratives.skip_titles`).",
          "- CV MAE fits w on the other weeks and scores the held-out week; compare it with MAE w=0.",
          f"- Leans with |lean| <= {ncfg.get('min_abs_lean', 0)} are ignored."]
    return "\n".join(L) + "\n"


def run(username: str, team_name: str, league_id: str | None, season: int | None, weeks: list[int],
        archive: Path, cfg: dict, out_dir: str | Path = "reports") -> Path:
    ncfg = cfg.get("narratives", {})
    lg = sleeper.find_my_team(username, team_name, season or int(sleeper.nfl_state()["season"]), league_id)[0]
    season = season or int(lg.get("season") or sleeper.nfl_state()["season"])
    scoring = lg.get("scoring_settings") or {}
    rec = float(scoring.get("rec", 0))
    rows = collect_rows(Path(archive), season, weeks, kickoffs(season), ncfg.get("skip_titles", []))
    rows = [r for r in rows if r.pre >= ncfg.get("min_pre", 1.0)]
    print(f"  - {len(rows)} player-weeks, weeks {weeks[0]}-{weeks[-1]}", flush=True)
    actuals(rows, season, scoring, rec, lg.get("league_id"))

    cap = ncfg.get("max_articles_per_player", 15)
    links = sorted({a["link"] for r in rows for a in r.articles[:cap] if a.get("link")})
    print(f"  - fetching {len(links)} articles", flush=True)
    with ThreadPoolExecutor(ncfg.get("fetch_workers", 3)) as pool:
        pages = dict(zip(links, pool.map(lambda l: fetch(l, ncfg), links)))

    out = Path(out_dir) / "narratives"
    out.mkdir(parents=True, exist_ok=True)
    cache_f = out / "llm_cache.json"
    cache = json.loads(cache_f.read_text()) if cache_f.exists() else {}
    funnel = {"player-weeks": len(rows), "articles (pre-kickoff, not hub pages)": sum(len(r.articles[:cap]) for r in rows),
              "unique links": len(links), "fetched": sum("error" not in p for p in pages.values()),
              "edited after kickoff (dropped)": 0, "with paragraphs naming the player": 0}
    todo = []
    for r in rows:
        items = []
        for a in r.articles[:cap]:
            pg = pages.get(a.get("link"), {})
            if "error" in pg or not pg.get("text"):
                continue
            if pg.get("modified") and pg["modified"] >= r.kickoff:
                funnel["edited after kickoff (dropped)"] += 1
                continue
            ex = excerpt(pg["text"], r.name, ncfg.get("excerpt_chars", 1500))
            if ex:
                items.append({"title": a["title"], "source": a.get("source"), "url": pg["url"], "excerpt": ex,
                              "published": a.get("published")})
        funnel["with paragraphs naming the player"] += len(items)
        todo.append((r, items))
    print(f"  - reading {funnel['with paragraphs naming the player']} excerpts with the LLM", flush=True)
    with ThreadPoolExecutor(ncfg.get("llm", {}).get("workers", 5)) as pool:
        calls = list(pool.map(lambda t: read_calls(t[0], t[1], ncfg, cache), todo))
    cache_f.write_text(json.dumps(cache))
    funnel["player-weeks the LLM couldn't read"] = sum(c is None for c in calls)

    min_lean = ncfg.get("min_abs_lean", 0.0)
    recs, call_rows = [], []
    for (r, items), cs in zip(todo, calls):
        cs = cs or []
        for it, c in zip(items, cs):
            call_rows.append({"week": r.week, "player": r.name, "pos": r.position, "source": it["source"],
                              "title": it["title"], "url": it["url"], **c})
        ok = [c for c in cs if c["week"] in (None, r.week) and abs(c["lean"]) > min_lean]
        call = [c["lean"] for c in ok if c["kind"] == "call"]
        both = [c["lean"] for c in ok if c["kind"] in ("call", "outlook")]
        recs.append({"week": r.week, "player_id": r.player_id, "name": r.name, "pos": r.position, "group": r.group,
                     "run": r.run, "pre": r.pre, "actual": r.actual, "sentiment": r.sentiment,
                     "narrative": float(np.mean(call)) if call else 0.0, "n_calls": len(call),
                     "narrative_outlook": float(np.mean(both)) if both else 0.0, "n_outlook": len(both),
                     "n_articles": len(r.articles), "n_read": len(items)})
    funnel["calls about this week"] = sum(x["n_calls"] for x in recs)
    funnel["player-weeks with at least one call"] = sum(x["n_calls"] > 0 for x in recs)

    grid = ncfg.get("weights", [0, 0.04, 0.08, 0.15, 0.25, 0.5])
    cols = lambda sub, k: ([x["pre"] for x in sub], [x["actual"] for x in sub], [x[k] for x in sub],
                           [x["week"] for x in sub], [x["player_id"] for x in sub])
    with_call = [x for x in recs if x["n_calls"]]
    with_any = [x for x in recs if x["n_outlook"]]
    nb = ncfg.get("bootstrap", 4000)
    ev = {"narrative (calls)": evaluate(*cols(with_call, "narrative"), grid, nb),
          "headline sentiment, same rows": evaluate(*cols(with_call, "sentiment"), grid, nb),
          "narrative+outlook": evaluate(*cols(with_any, "narrative_outlook"), grid, nb),
          "narrative, all rows (0 if no call)": evaluate(*cols(recs, "narrative"), grid, nb)}
    label = f"{season} weeks {weeks[0]}-{weeks[-1]}"
    md = out / "narratives.md"
    md.write_text(render(ev, funnel, label, grid, ncfg))
    for name, data in (("rows.csv", recs), ("calls.csv", call_rows)):
        if data:
            with open(out / name, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(data[0]))
                w.writeheader()
                w.writerows(data)
    errs: dict[str, int] = {}
    for p in pages.values():
        if "error" in p:
            k = re.sub(r"for url: .*", "", p["error"])[:80]
            errs[k] = errs.get(k, 0) + 1
    (out / "fetch_errors.json").write_text(json.dumps(errs, indent=1))
    return md
