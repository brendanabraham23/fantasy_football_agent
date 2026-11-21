"""Narrative backtest: row selection, Google News decoding, excerpts, evaluation and an offline end-to-end run."""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from ffa import http, llm, narratives, sleeper

KO = datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc)
SKIP = ["News, Rankings, Projections"]


def _player(name, team="KC", pos="WR", adj=11.0, ms=1.1, arts=()):
    return {"name": name, "position": pos, "team": team, "on_bye": False, "adj": adj, "mult_sentiment": ms,
            "sentiment": 1.25, "group": "roster", "articles": list(arts)}


def _art(title, published, link="https://news.google.com/rss/articles/ABC?oc=5"):
    return {"title": title, "source": "Site", "link": link, "published": published.isoformat()}


def _archive(tmp_path, runs):
    for name, gen, players in runs:
        d = tmp_path / name
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"meta": {"generated_at": gen.isoformat()}, "players": players}))
    return tmp_path


def test_collect_rows_uses_newest_pre_kickoff_run_and_pre_kickoff_articles(tmp_path):
    early, late, after = KO - timedelta(days=2), KO - timedelta(hours=1), KO + timedelta(hours=1)
    arts = [_art("Start him this week", KO - timedelta(hours=5)), _art("He went off", KO + timedelta(hours=4)),
            _art("Echo WR Fantasy Football News, Rankings, Projections | Chiefs", KO - timedelta(days=1))]
    arch = _archive(tmp_path, [
        ("2026-10-16_000000_week06_2026", early, {"5": _player("Echo WR", adj=5.0, ms=1.0, arts=arts[:1])}),
        ("2026-10-18_160000_week06_2026", late, {"5": _player("Echo WR", arts=arts)}),
        ("2026-10-18_180000_week06_2026", after, {"5": _player("Echo WR", arts=arts)}),
        ("2026-10-25_160000_week07_2026", late + timedelta(days=7), {"5": _player("Echo WR", arts=arts)}),
    ])
    rows = narratives.collect_rows(arch, 2026, [6], {(6, "KC"): KO}, SKIP)
    assert len(rows) == 1
    r = rows[0]
    assert r.run == "2026-10-18_160000_week06_2026" and r.pre == pytest.approx(10.0)
    assert [a["title"] for a in r.articles] == ["Start him this week"]


def test_resolve_decodes_google_news_links(monkeypatch):
    page = '<c-wiz><div jscontroller="x" data-n-a-sg="SIG123" data-n-a-ts="1730000000"></div></c-wiz>'
    inner = json.dumps(["garturlres", "https://example.com/story", 1])
    body = ")]}'\n\n" + json.dumps([["wrb.fr", "Fbv4je", inner, None, None, None, "generic"], ["di", 10], ["af", 1]])
    seen = {}
    monkeypatch.setattr(http, "get_text", lambda url, **k: page)
    monkeypatch.setattr(http, "post", lambda url, data, **k: seen.update(data) or body.encode())
    assert narratives.resolve("https://news.google.com/rss/articles/CBMiXYZ?oc=5", 0) == "https://example.com/story"
    assert "CBMiXYZ" in seen["f.req"] and "SIG123" in seen["f.req"]
    assert narratives.resolve("https://www.espn.com/a", 0) == "https://www.espn.com/a"


def test_page_dates_and_excerpt():
    html = ('<meta property="article:published_time" content="2026-10-17T12:00:00Z">'
            '<script>{"datePublished":"2026-10-17T12:00:00Z","dateModified":"2026-10-18T19:30:00+00:00"}</script>')
    d = narratives.page_dates(html)
    assert d["published"] == datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    assert d["modified"] == datetime(2026, 10, 18, 19, 30, tzinfo=timezone.utc)
    text = "Intro about the week.\nJaxon Smith-Njigba, WR\nStart him: smash spot vs a weak secondary.\nOther guy.\nMore."
    ex = narratives.excerpt(text, "Jaxon Smith-Njigba", 500)
    assert ex == "Jaxon Smith-Njigba, WR\nStart him: smash spot vs a weak secondary."
    assert narratives.excerpt("Paragraph one.\nTwo.", "Kenneth Walker III", 500) == ""


def test_evaluate_recovers_a_real_signal():
    rng = np.random.default_rng(1)
    n = 200
    pre = rng.uniform(5, 20, n)
    s = rng.uniform(-1, 1, n)
    act = pre * (1 + 0.3 * s) + rng.normal(0, 0.5, n)
    e = narratives.evaluate(pre, act, s, np.arange(n) % 5, np.arange(n) % 40, [0, 0.3], n_boot=300)
    assert e["w"] == pytest.approx(0.3, abs=0.05) and e["ci"][0] > 0 and e["p_pos"] == 1.0
    assert e["cv_mae"] < e["base_mae"] and e["mae"][0.3] < e["mae"][0]
    assert e["beat_pos"][0] > e["beat_neg"][0]


def test_run_end_to_end_offline(tmp_path, monkeypatch):
    gen = KO - timedelta(hours=2)
    players = {"5": _player("Echo WR", arts=[_art("Week 6 start/sit", KO - timedelta(hours=6), "https://a.com/1"),
                                             _art("Edited later", KO - timedelta(hours=6), "https://a.com/2"),
                                             _art("Broken link", KO - timedelta(hours=6), "https://a.com/3")]),
               "6": _player("Foxtrot WR", team="BUF", arts=[_art("BUF notes", KO - timedelta(hours=6), "https://a.com/1")])}
    arch = _archive(tmp_path / "archive", [("2026-10-18_150000_week06_2026", gen, players)])
    monkeypatch.setattr(sleeper, "find_my_team", lambda *a: ({"league_id": "L1", "season": "2026",
                                                              "scoring_settings": {"rec": 1.0, "rec_yd": 0.1}},))
    monkeypatch.setattr(sleeper, "stats", lambda s, w: {"5": {"rec": 6, "rec_yd": 90, "gp": 1}})  # 15 pts; 6 didn't play
    monkeypatch.setattr(sleeper, "matchups", lambda lid, w, **k: [])
    monkeypatch.setattr(narratives, "kickoffs", lambda season: {(6, "KC"): KO, (6, "BUF"): KO})
    pages = {"https://a.com/1": {"url": "https://a.com/1", "text": "Echo WR is a must-start.\nFoxtrot WR is out.",
                                 "modified": None, "published": None},
             "https://a.com/2": {"url": "https://a.com/2", "text": "Echo WR exploded.", "modified": KO + timedelta(hours=5),
                                 "published": None},
             "https://a.com/3": {"url": "https://a.com/3", "error": "HTTPError: 403 Client Error: Forbidden for url: x"}}
    monkeypatch.setattr(narratives, "fetch", lambda link, ncfg: pages[link])
    prompts = []

    def fake_call(cmd, model, prompt, timeout, system, schema):
        prompts.append(prompt)
        lean = 0.7 if "Echo" in prompt.split("\n")[0] else -0.9
        return {"articles": [{"id": 0, "kind": "call", "lean": lean, "week": 6, "quote": "must-start"}]}
    monkeypatch.setattr(llm, "_call", fake_call)

    cfg = {"narratives": {"skip_titles": SKIP, "bootstrap": 50, "weights": [0, 0.1], "llm": {"workers": 1}}}
    md = narratives.run("me", "Brendobendo", None, 2026, [6], arch, cfg, tmp_path)
    text = md.read_text()
    assert "| edited after kickoff (dropped) | 1 |" in text and "| fetched | 2 |" in text
    assert len(prompts) == 2 and all("Edited later" not in p for p in prompts)
    rows = (tmp_path / "narratives" / "rows.csv").read_text().splitlines()
    echo = next(l for l in rows if "Echo WR" in l).split(",")
    fox = next(l for l in rows if "Foxtrot WR" in l).split(",")
    assert echo[7] == "15.0" and echo[9] == "0.7" and fox[7] == "0.0"
    errs = json.loads((tmp_path / "narratives" / "fetch_errors.json").read_text())
    assert errs == {"HTTPError: 403 Client Error: Forbidden ": 1}
    # a rerun reads from the LLM cache instead of calling again
    narratives.run("me", "Brendobendo", None, 2026, [6], arch, cfg, tmp_path)
    assert len(prompts) == 2
