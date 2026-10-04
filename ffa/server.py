"""Local web UI: FastAPI JSON API over the latest run snapshot and live (cached) Sleeper data.

    python -m ffa ui --username <sleeper_username>
"""
from __future__ import annotations

import copy
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import nflverse, pipeline, report, sleeper, snapshot, waivers
from .lineup import best_total, player_positions
from .news import NewsScorer
from .ranker import Evaluator, PlayerEval, optimal_lineup, player_name

WEB = Path(__file__).resolve().parent / "web"
INJURY_KEYS = ("injury_status", "injury_body_part", "injury_notes")
KEY_STATS = ["pass_yd", "pass_td", "pass_int", "rush_att", "rush_yd", "rush_td", "rec_tgt", "rec", "rec_yd",
             "rec_td", "fgm", "fga", "xpm"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    state: str = "idle"            # idle | running | done | error
    started_at: str | None = None
    finished_at: str | None = None
    options: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    error: str | None = None
    thread: threading.Thread | None = None

    def view(self) -> dict:
        return {"state": self.state, "started_at": self.started_at, "finished_at": self.finished_at,
                "options": self.options, "log": self.log[-200:], "error": self.error}


class Live:
    """A Context built from cached network data, plus lazily fetched per-week stats/projections.

    `as_of=True` means ctx.week is a completed past week (an archived run being reviewed): nothing after
    that week is shown, rosters come from that week's matchups, and current-only signals (trending adds,
    injury designations) are blanked so they can't leak into the view. Actual points for ctx.week are shown.
    """

    def __init__(self, ctx: pipeline.Context, as_of: bool = False):
        self.ctx, self.as_of, self.built = ctx, as_of, time.time()
        self._stats, self._proj, self._evals, self._matchups, self._actuals = {}, {}, {}, {}, {}
        self._sched = None
        self._pool = None
        self._lock = threading.Lock()
        self.warnings: list[str] = []
        if as_of:
            self._rewind()
        self.ev = Evaluator(ctx)
        self.owners = ctx.owners()
        self.mine = {pid for key in ("players", "reserve", "taxi") for pid in (ctx.my_roster.get(key) or [])}

    @property
    def through(self) -> int:
        """Last week whose actual results may be shown."""
        return self.ctx.week if self.as_of else self.ctx.week - 1

    def _rewind(self):
        c = self.ctx
        c.trending = {}
        c.players = {pid: ({k: v for k, v in p.items() if k not in INJURY_KEYS}
                           if any(p.get(k) for k in INJURY_KEYS) else p) for pid, p in c.players.items()}
        by_rid = {m.get("roster_id"): m for m in self.matchups(c.week)}
        if not by_rid:
            print(f"[warn] no week {c.week} matchups; showing current rosters")
            return
        for r in c.rosters:
            m = by_rid.get(r.get("roster_id"))
            if m:
                r.update(players=list(m.get("players") or []), starters=list(m.get("starters") or []),
                         reserve=[], taxi=[])

    def matchups(self, week: int) -> list[dict]:
        if week not in self._matchups:
            try:
                self._matchups[week] = sleeper.matchups(self.ctx.league["league_id"], week)
            except Exception as exc:
                print(f"[warn] matchups unavailable for week {week} ({exc})")
                self._matchups[week] = []
        return self._matchups[week]

    def actuals(self, week: int) -> dict[str, float]:
        """player_id -> actual fantasy points in league scoring (Sleeper's own numbers for rostered players)."""
        if week not in self._actuals:
            out = {}
            for pid, line in self.stats(week).items():
                pos = (self.ctx.players.get(pid) or {}).get("position")
                if pos and (line.get("gp", 1) or 0) > 0 and (pts := self.points(line, pos)) is not None:
                    out[pid] = pts
            for m in self.matchups(week):
                out.update({pid: round(float(v), 2) for pid, v in (m.get("players_points") or {}).items()})
            self._actuals[week] = out
        return self._actuals[week]

    def stats(self, week: int) -> dict:
        if week not in self._stats:
            self._stats[week] = self.ctx.recent.get(week) or sleeper.stats(self.ctx.season, week)
        return self._stats[week]

    def proj(self, week: int) -> dict:
        if week not in self._proj:
            self._proj[week] = self.ctx.projections if week == self.ctx.week else \
                sleeper.projections(self.ctx.season, week)
        return self._proj[week]

    def schedule(self):
        if self._sched is None:
            try:
                self._sched = nflverse.schedule(self.ctx.season)
            except Exception as exc:
                print(f"[warn] schedule unavailable ({exc})")
                self._sched = pd.DataFrame()
        return self._sched

    def evaluate(self, pid: str) -> PlayerEval:
        with self._lock:
            if pid not in self._evals:
                self._evals[pid] = self.ev.evaluate(pid)
            return self._evals[pid]

    def owner(self, pid: str) -> str | None:
        return "Mine" if pid in self.mine else self.owners.get(pid)

    def points(self, line: dict | None, pos: str) -> float | None:
        return sleeper.fantasy_points(line, self.ctx.scoring, self.ctx.rec_value, pos)


class RunRequest(BaseModel):
    news: bool = True
    weather: bool = True


def create_app(username: str, team_name: str = "", league_id: str | None = None, week: int | None = None,
               cfg: dict | None = None, out_dir: str | Path = "reports") -> FastAPI:
    cfg = cfg or pipeline.load_config()
    ucfg = cfg.get("ui", {})
    out_dir = Path(out_dir)
    app = FastAPI(title="Fantasy Football Analyzer", docs_url="/api/docs", openapi_url="/api/openapi.json")
    job = Job()
    live_box: dict = {}
    live_lock = threading.Lock()
    app.state.job = job

    # ---- helpers ----------------------------------------------------------------
    archive_dir = cfg.get("archive_dir", "archive")

    def snap() -> dict | None:
        return snapshot.load(out_dir)

    def snap_or_run(run: str | None) -> dict:
        """The latest snapshot, or an archived one when `run` is given (404 if missing)."""
        if run:
            s = snapshot.load_run(out_dir, run, archive_dir)
            if not s:
                raise HTTPException(404, f"No archived run '{run}'")
            return s
        s = snap()
        if not s:
            raise HTTPException(404, "No pipeline runs yet. Click Run pipeline to create one.")
        return s

    def current() -> tuple[int, int] | None:
        try:
            state = sleeper.nfl_state()
            return int(state["season"]), week or int(state.get("display_week") or state["week"])
        except Exception as exc:
            print(f"[warn] nfl state unavailable ({exc})")
            return None

    def live(run: str | None = None) -> Live:
        """Live data for now, or rewound to the week of archived run `run` once that week is over."""
        key, season, wk, as_of = "live", None, week, False
        if run:
            m = snap_or_run(run)["meta"]
            cur = current()
            if cur and (m["season"], m["week"]) < cur:
                key, season, wk, as_of = (m["season"], m["week"]), m["season"], m["week"], True
        ttl = ucfg.get("live_ttl_minutes", 15) * 60
        with live_lock:
            lv = live_box.get(key)
            if lv is None or time.time() - lv.built > ttl:
                try:
                    with snapshot.capture() as lines:
                        ctx = pipeline.build_context(username, team_name, league_id, season, wk, copy.deepcopy(cfg))
                        lv = Live(ctx, as_of)
                except HTTPException:
                    raise
                except Exception as exc:
                    raise HTTPException(502, f"Couldn't load live data from Sleeper/nflverse: {exc}")
                lv.warnings = snapshot.warnings_from(lines)
                live_box[key] = lv
            return lv

    def snap_for(lv: Live, run: str | None = None) -> dict | None:
        s = snap_or_run(run) if run else snap()
        if s and s["meta"]["season"] == lv.ctx.season and s["meta"]["week"] == lv.ctx.week:
            return s
        return None

    def as_of_info(lv: Live) -> dict:
        return {"as_of": lv.as_of, "season": lv.ctx.season, "week": lv.ctx.week, "through": lv.through}

    def outcome(lv: Live, s: dict) -> dict:
        """Predictions in snapshot `s` (a completed week) against what actually happened."""
        w, P = lv.ctx.week, s["players"]
        act = lv.actuals(w)
        actual = {pid: act.get(pid) for pid in P}
        rec_ids = [row["player_id"] for row in s["lineup"] if row["player_id"]]
        my_m = next((m for m in lv.matchups(w) if m.get("roster_id") == lv.ctx.my_roster.get("roster_id")), None)
        played = [p for p in ((my_m or {}).get("starters") or s["current_starters"]) if p and p != "0"]
        roster = set((my_m or {}).get("players") or
                     [pid for pid, d in P.items() if d.get("group") in ("roster", "reserve")])
        positions = player_positions(lv.ctx.players)
        errs = [act[pid] - d["adj"] for pid, d in P.items()
                if d.get("group") == "roster" and pid in act and not d.get("on_bye")]
        return {
            "week": w, "actual": actual,
            "lineup": {"projected": s["total"], "recommended": round(sum(act.get(p, 0) for p in rec_ids), 2),
                       "played": round(sum(act.get(p, 0) for p in played), 2), "played_ids": played,
                       "best": best_total(roster, act, positions, s["meta"]["roster_positions"])},
            "errors": {"n": len(errs), "mae": round(sum(abs(e) for e in errs) / len(errs), 2) if errs else None,
                       "bias": round(sum(errs) / len(errs), 2) if errs else None},
        }

    def my_evals(lv: Live, run: str | None = None) -> tuple[list[PlayerEval], set[str]]:
        """Active roster evals (snapshot when current, with sentiment; else live) and the optimal starters."""
        s = snap_for(lv, run)
        if s:
            evals = [snapshot.player_from_dict(d) for d in s["players"].values() if d.get("group") == "roster"]
        else:
            reserve = set(lv.ctx.my_roster.get("reserve") or []) | set(lv.ctx.my_roster.get("taxi") or [])
            evals = [lv.evaluate(p) for p in (lv.ctx.my_roster.get("players") or []) if p not in reserve]
        starters = {e.player_id for _, e in optimal_lineup(evals, lv.ctx.league["roster_positions"]) if e}
        return evals, starters

    # ---- run job ----------------------------------------------------------------
    def run_job(opts: dict):
        try:
            with snapshot.capture(job.log):
                print("Running fantasy analysis...")
                res = pipeline.run(username, team_name, league_id, None, week, copy.deepcopy(cfg),
                                   news=opts["news"], wx=opts["weather"])
                data = snapshot.to_dict(res, snapshot.warnings_from(job.log), opts)
                md, csv = report.save(res, out_dir)
                run_dir = snapshot.save(data, out_dir, (md, csv), archive_dir)
                print(f"Saved report and snapshot; archived to {run_dir}")
            job.state = "done"
        except Exception as exc:
            traceback.print_exc()
            job.error, job.state = f"{type(exc).__name__}: {exc}", "error"
        finally:
            job.finished_at = _now()
            live_box.pop("live", None)

    @app.post("/api/run", status_code=202)
    def start_run(req: RunRequest | None = None):
        req = req or RunRequest()
        if job.state == "running":
            return JSONResponse(job.view(), status_code=409)
        job.state, job.started_at, job.finished_at, job.error = "running", _now(), None, None
        job.options, job.log[:] = {"news": req.news, "weather": req.weather}, []
        job.thread = threading.Thread(target=run_job, args=(job.options,), daemon=True)
        job.thread.start()
        return job.view()

    @app.get("/api/run")
    def run_status():
        return job.view()

    # ---- summary ----------------------------------------------------------------
    @app.get("/api/summary")
    def summary(run: str | None = None):
        s = snap_or_run(run)
        cur = current()
        current_week = cur[1] if cur and cur[0] == s["meta"]["season"] else (99 if cur and cur[0] > s["meta"]["season"] else None)
        gen = datetime.fromisoformat(s["meta"]["generated_at"])
        age_h = (datetime.now(timezone.utc) - gen).total_seconds() / 3600
        stale = age_h > ucfg.get("stale_hours", 24) or (current_week is not None and s["meta"]["week"] < current_week)
        out = {**s, "stale": stale, "age_hours": round(age_h, 1), "current_week": current_week,
               "run_id": run, "archived": bool(run), "outcome": None}
        if run and current_week is not None and s["meta"]["week"] < current_week:
            try:
                lv = live(run)
                if lv.as_of:
                    out["outcome"] = outcome(lv, s)
            except HTTPException as exc:
                out["outcome_error"] = exc.detail
        return out

    # ---- archive ----------------------------------------------------------------
    @app.get("/api/runs")
    def runs():
        return snapshot.list_runs(out_dir, archive_dir)

    @app.get("/api/compare")
    def compare(run: str, to: str | None = None):
        """Diff archived run `run` against `to` (another archived run) or the latest snapshot."""
        return snapshot.compare(snap_or_run(run), snap_or_run(to))

    # ---- players ----------------------------------------------------------------
    @app.get("/api/players/search")
    def search(q: str = "", limit: int = Query(None, ge=1, le=100), run: str | None = None):
        lv = live(run)
        limit = limit or ucfg.get("search_limit", 15)
        ql = q.strip().lower()
        if not ql:
            return []
        rostered = lv.ctx.rostered
        hits = []
        for pid, p in lv.ctx.players.items():
            if p.get("position") not in sleeper.FANTASY_POSITIONS or not (p.get("team") or pid in rostered):
                continue
            name = player_name(p, pid)
            low = name.lower()
            if ql not in low:
                continue
            prefix = low.startswith(ql) or any(part.startswith(ql) for part in low.split())
            hits.append((0 if prefix else 1, p.get("search_rank") or 10**6, name, pid, p))
        hits.sort(key=lambda h: h[:3])
        return [{"player_id": pid, "name": name, "position": p.get("position"),
                 "team": sleeper.norm_team(p.get("team")), "owner": lv.owner(pid)}
                for _, _, name, pid, p in hits[:limit]]

    @app.get("/api/players/{pid}")
    def player(pid: str, run: str | None = None):
        lv = live(run)
        p = lv.ctx.players.get(pid)
        if not p:
            raise HTTPException(404, f"Unknown player {pid}")
        pos = p.get("position") or "?"
        team = sleeper.norm_team(p.get("team"))
        sched = lv.schedule()
        history = []
        for w in range(1, lv.ctx.week + 1):
            g = None
            if team and not sched.empty:
                rows = sched[(sched["week"] == w) & (sched["game_type"] == "REG")
                             & ((sched["home_team"] == team) | (sched["away_team"] == team))]
                if not rows.empty:
                    r = rows.iloc[0]
                    g = ("vs " if r["home_team"] == team else "@ ") + (r["away_team"] if r["home_team"] == team
                                                                      else r["home_team"])
            line = lv.stats(w).get(pid) if w <= lv.through else None
            played = bool(line) and (line.get("gp", 1) or 0) > 0
            history.append({
                "week": w, "opp": g or ("BYE" if team and not sched.empty else None),
                "bye": bool(team and not sched.empty and g is None),
                "actual": (lv.actuals(w).get(pid) if lv.as_of and w == lv.ctx.week else lv.points(line, pos))
                          if played else None,
                "projected": lv.points(lv.proj(w).get(pid), pos),
                "stats": {k: line[k] for k in KEY_STATS if played and isinstance(line.get(k), (int, float))},
                "current": w == lv.ctx.week,
            })
        s = snap_for(lv, run)
        if s and pid in s["players"]:
            ev, source = s["players"][pid], "snapshot"
        else:
            ev, source = snapshot.player_dict(lv.evaluate(pid)), "live"
        played = [h["actual"] for h in history if h["actual"] is not None and h["week"] < lv.ctx.week]
        return {
            "player_id": pid, "name": player_name(p, pid), "position": pos, "team": team,
            "injury_status": p.get("injury_status"), "injury_detail": p.get("injury_body_part") or p.get("injury_notes"),
            "age": p.get("age"), "years_exp": p.get("years_exp"), "number": p.get("number"),
            "owner": lv.owner(pid), "trending_adds": lv.ctx.trending.get(pid, 0),
            "season": lv.ctx.season, "week": lv.ctx.week, "history": history,
            "season_pts": round(sum(played), 2), "games": len(played),
            "eval": ev, "eval_source": source, **as_of_info(lv),
            "actual": lv.actuals(lv.ctx.week).get(pid) if lv.as_of else None,
        }

    # ---- waivers ----------------------------------------------------------------
    @app.get("/api/waivers/pool")
    def pool(run: str | None = None):
        lv = live(run)
        if lv._pool is not None and lv._pool["run"] == run:
            return lv._pool
        s = snap_for(lv, run)
        rec_ids = {r["player_id"] for r in s["waiver_recs"]} if s else set()
        rostered, positions = lv.ctx.rostered, lv.ctx.fantasy_positions
        past = [lv.stats(w) for w in range(1, lv.ctx.week)]
        rows = []
        for pid, p in lv.ctx.players.items():
            pos = p.get("position")
            if pos not in positions or pid in rostered or not p.get("team") or p.get("active") is False:
                continue
            games = [pts for wk in past if (line := wk.get(pid)) and (line.get("gp", 1) or 0) > 0
                     and (pts := lv.points(line, pos)) is not None]
            proj = lv.points(lv.ctx.projections.get(pid), pos)
            if not games and not proj and pid not in lv.ctx.trending:
                continue
            e = lv.evaluate(pid)
            rows.append({
                "player_id": pid, "name": e.name, "position": pos, "team": e.team, "opp": e.opp,
                "is_home": e.is_home, "on_bye": e.on_bye, "injury_status": e.injury_status,
                "proj": e.proj, "recent_avg": e.recent_avg, "adj": e.adj, "matchup_grade": e.matchup_grade,
                "season_pts": round(sum(games), 2), "games": len(games),
                "ppg": round(sum(games) / len(games), 2) if games else None,
                "trending_adds": e.trending_adds, "recommended": pid in rec_ids,
                "actual": lv.actuals(lv.ctx.week).get(pid) if lv.as_of else None,
            })
        rows.sort(key=lambda r: r["adj"], reverse=True)
        lv._pool = {"run": run, **as_of_info(lv), "players": rows}
        return lv._pool

    @app.get("/api/waivers/whatif/{pid}")
    def whatif(pid: str, run: str | None = None):
        lv = live(run)
        if pid not in lv.ctx.players:
            raise HTTPException(404, f"Unknown player {pid}")
        if pid in lv.ctx.rostered:
            raise HTTPException(400, f"{player_name(lv.ctx.players[pid], pid)} is already rostered")
        evals, starters = my_evals(lv, run)
        s = snap_for(lv, run)
        cand = snapshot.player_from_dict(s["players"][pid]) if s and pid in s["players"] else lv.evaluate(pid)
        r = waivers.evaluate_add(lv.ctx, evals, starters, cand)
        return {"player_id": pid, "weekly_gain": r.weekly_gain, "ros_gain": r.ros_gain, "bid": r.bid,
                "score": r.score, "drop": {"player_id": r.drop.player_id, "name": r.drop.name,
                                           "position": r.drop.position} if r.drop else None}

    # ---- news -------------------------------------------------------------------
    @app.get("/api/news")
    def news(run: str | None = None):
        s = snap_or_run(run)
        rec_ids = {r["player_id"] for r in s["waiver_recs"]}
        out = []
        for pid, d in s["players"].items():
            if d.get("group") != "roster" and not d.get("n_articles"):
                continue
            out.append({k: d.get(k) for k in ("player_id", "name", "position", "team", "group", "sentiment",
                                              "n_articles", "mult_sentiment", "articles", "headlines")}
                       | {"recommended": pid in rec_ids})
        out.sort(key=lambda d: abs(d["sentiment"] or 0), reverse=True)
        return {"news_enabled": s["meta"].get("options", {}).get("news", True),
                "sentiment_weight": s["meta"].get("sentiment_weight"), "players": out}

    @app.get("/api/news/{pid}")
    def player_news(pid: str):
        lv = live()
        p = lv.ctx.players.get(pid)
        if not p:
            raise HTTPException(404, f"Unknown player {pid}")
        name = player_name(p, pid)
        scorer = NewsScorer(cfg["news"])
        sent = scorer.score(name, scorer.player_articles(name))
        return {"player_id": pid, "name": name, "position": p.get("position"),
                "team": sleeper.norm_team(p.get("team")), "sentiment": sent.score, "n_articles": sent.n_articles,
                "mult_sentiment": round(1 + cfg["sentiment_weight"] * sent.score, 3), "articles": sent.articles}

    @app.get("/api/config")
    def ui_config():
        return {"team_name": team_name, "ui": ucfg}

    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app


def serve(username: str, team_name: str, league_id: str | None, week: int | None, cfg: dict,
          out_dir: str, host: str | None = None, port: int | None = None) -> None:
    import uvicorn
    ucfg = cfg.get("ui", {})
    host, port = host or ucfg.get("host", "127.0.0.1"), port or ucfg.get("port", 8000)
    print(f"Fantasy UI on http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(username, team_name, league_id, week, cfg, out_dir), host=host, port=port,
                log_level="warning")
