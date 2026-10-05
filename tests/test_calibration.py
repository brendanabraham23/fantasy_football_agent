"""Calibration report on synthetic data with a known miscalibration (offline)."""
import numpy as np
import pandas as pd

from ffa import calibration, pipeline, sleeper


def test_calibration_recovers_known_miscalibration(monkeypatch, tmp_path):
    rng = np.random.default_rng(0)
    players = {str(i): {"full_name": f"P{i}", "position": ["QB", "RB", "WR", "TE"][i % 4], "team": "KC"} for i in range(80)}
    talent = {pid: rng.uniform(4, 24) for pid in players}
    line = lambda pts, gp=1: {"pts_ppr": round(pts, 2), "gp": gp}
    proj = {wk: {pid: line(t) for pid, t in talent.items()} for wk in range(1, 9)}
    # truth: projections are too spread out (actual = 2 + 0.7 x proj) and 1 in 20 players sits out
    stats = {wk: {pid: line(max(0, 2 + 0.7 * t + rng.normal(0, 3))) if rng.random() > 0.05 else line(0, gp=0)
                  for pid, t in talent.items()} for wk in range(1, 9)}
    monkeypatch.setattr(sleeper, "all_players", lambda: players)
    monkeypatch.setattr(sleeper, "projections", lambda s, w: proj[w])
    monkeypatch.setattr(sleeper, "stats", lambda s, w: stats.get(w, {}))
    monkeypatch.setattr(sleeper, "nfl_state", lambda: {"season": "2026", "week": 9})

    cfg = pipeline.load_config()
    md, csv = calibration.run([2026], [4, 5, 6, 7, 8], cfg, tmp_path)
    df = pd.read_csv(csv)
    played = df[df.played & (df.sleeper >= 3)]
    s = calibration.metrics(played, "sleeper")
    assert 0.6 < s["slope"] < 0.8 and s["bias"] > 0                       # recovers the planted shrinkage
    assert calibration.metrics(played, "recent")["n"] == len(played)     # weeks 1-3 seed recent form
    w, _ = calibration.best_blend(played)
    assert w < 0.5            # projections carry no info beyond talent here, and recent form is already shrunk
    b = calibration.buckets(df[df.position == "WR"], "sleeper", 5)
    assert len(b) == 5 and b["predicted"].is_monotonic_increasing and (b["ratio"].iloc[-1] < b["ratio"].iloc[0])
    text = md.read_text()
    assert "too spread out" in text and "Calibration buckets" in text and "### QB" in text
    assert df["played"].mean() < 1 and (df.loc[~df.played, "actual"] == 0).all()
