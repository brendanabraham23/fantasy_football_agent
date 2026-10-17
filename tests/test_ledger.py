"""Ledger + exact lineup tests (offline)."""
import pytest

from ffa import ledger, sleeper
from ffa.ledger import WeekData, build_ledger, max_adds_check, moves_per_team
from ffa.lineup import best_lineup

SLOTS = ["QB", "RB", "WR", "FLEX", "BN", "BN", "BN"]
POS = {"qb": {"QB"}, "rb1": {"RB"}, "rb2": {"RB"}, "wr1": {"WR"}, "wr2": {"WR"},
       "fa_rb": {"RB"}, "fa_wr": {"WR"}, "x_wr": {"WR"}}


def test_exact_lineup_beats_greedy_on_overlapping_flex():
    pts = {"wr": 10, "te": 9, "rb": 2}
    pos = {"wr": {"WR"}, "te": {"TE"}, "rb": {"RB"}}
    total, lineup = best_lineup(pts, pos, ["REC_FLEX", "WRRB_FLEX"])
    assert total == 19 and dict(lineup) == {"REC_FLEX": "te", "WRRB_FLEX": "wr"}


def test_lineup_leaves_slot_empty_rather_than_negative():
    total, lineup = best_lineup({"qb": -2.0}, {"qb": {"QB"}}, ["QB"])
    assert total == 0 and lineup == [("QB", None)]


def _txn(tid, typ, adds, drops=None, status="complete", bid=None, ts=0):
    t = {"transaction_id": tid, "type": typ, "status": status, "adds": adds, "drops": drops or {},
         "status_updated": ts}
    if bid is not None:
        t["settings"] = {"waiver_bid": bid}
    return t


@pytest.fixture
def season():
    base = {"qb": 20, "rb1": 15, "rb2": 3, "wr1": 12, "wr2": 4, "fa_rb": 0, "fa_wr": 0, "x_wr": 8}
    # roster 1 claims fa_rb in week 2 (drops rb2), then drops fa_rb for fa_wr in week 4
    weeks = {
        1: WeekData({1: {"qb", "rb1", "rb2", "wr1", "wr2"}, 2: {"x_wr"}}, {**base}),
        2: WeekData({1: {"qb", "rb1", "fa_rb", "wr1", "wr2"}, 2: {"x_wr"}}, {**base, "fa_rb": 14}),
        3: WeekData({1: {"qb", "rb1", "fa_rb", "wr1", "wr2"}, 2: {"x_wr"}}, {**base, "rb1": 0, "fa_rb": 18}),
        4: WeekData({1: {"qb", "rb1", "fa_wr", "wr1", "wr2"}, 2: {"x_wr", "fa_rb"}}, {**base, "fa_rb": 9, "fa_wr": 7}),
    }
    txns = {
        1: [],
        2: [_txn("t1", "waiver", {"fa_rb": 1}, {"rb2": 1}, bid=12, ts=1),
            _txn("t1x", "waiver", {"fa_rb": 2}, status="failed", bid=5, ts=1)],
        3: [],
        4: [_txn("t2", "free_agent", {"fa_wr": 1}, {"fa_rb": 1}, ts=2),
            _txn("t3", "waiver", {"fa_rb": 2}, bid=1, ts=3)],
    }
    return txns, weeks


def test_ledger_values_multi_week_and_stops_when_dropped(season):
    txns, weeks = season
    es = {e.transaction_id: e for e in build_ledger(txns, weeks, SLOTS, POS)}

    t1 = es["t1"]
    # wk2: fa_rb (14) takes FLEX; undone, wr2 (4) would have -> +10
    # wk3: rb1 scores 0, so fa_rb (18) starts at RB; undone, rb2 (3) would -> +15
    assert t1.weekly == {2: 10.0, 3: 15.0}             # stops in wk4 (fa_rb dropped)
    assert t1.value_total == 25 and t1.value_1wk == 10
    assert t1.bid == 12 and t1.runner_up_bid == 5 and t1.overpay_ratio == 2.4
    assert t1.pts_per_dollar == round(25 / 12, 2)

    # wk4 swap: fa_wr (7) in FLEX vs keeping fa_rb (9) -> -2
    assert es["t2"].weekly == {4: -2.0}
    # the failed claim is not a ledger entry; the later successful claim by roster 2 is
    assert "t1x" not in es and es["t3"].roster_id == 2


def test_trade_creates_one_entry_per_side():
    weeks = {1: WeekData({1: {"qb", "x_wr"}, 2: {"wr1"}}, {"qb": 20, "x_wr": 8, "wr1": 12})}
    txns = {1: [_txn("tr", "trade", {"x_wr": 1, "wr1": 2}, {"wr1": 1, "x_wr": 2})]}
    es = build_ledger(txns, weeks, ["QB", "WR"], POS)
    by_side = {e.roster_id: e for e in es}
    assert by_side[1].weekly == {1: -4.0}               # got 8, gave up 12
    assert by_side[2].weekly == {1: 4.0}                # got 12, gave up 8


def test_move_made_late_counts_from_next_week():
    weeks = {1: WeekData({1: {"qb"}}, {"qb": 20}),
             2: WeekData({1: {"qb", "wr1"}}, {"qb": 20, "wr1": 10})}
    txns = {1: [_txn("late", "free_agent", {"wr1": 1})], 2: []}
    (e,) = build_ledger(txns, weeks, ["QB", "WR"], POS)
    assert e.weekly == {2: 10.0}


def test_moves_per_team_and_max_adds_check(season):
    txns, _ = season
    moves = moves_per_team(txns, [1, 2], [1, 2, 3, 4])
    assert moves.set_index(["roster_id", "week"])["adds"].to_dict() == {
        (1, 1): 0, (1, 2): 1, (1, 3): 0, (1, 4): 1, (2, 1): 0, (2, 2): 0, (2, 3): 0, (2, 4): 1}
    check = max_adds_check(moves, 3, top_roster_ids={1})
    assert check["mean"] == 0.38 and check["max"] == 1 and check["share_over_max_adds"] == 0
    assert check["top_half_mean"] == 0.5


def test_ledger_run_end_to_end(monkeypatch, tmp_path, season):
    txns, weeks = season
    lg = {"league_id": "L1", "season": "2026", "name": "Test", "roster_positions": SLOTS,
          "scoring_settings": {"rush_yd": 0.1}}
    users = [{"user_id": "u1", "metadata": {"team_name": "Brendobendo"}},
             {"user_id": "u2", "display_name": "rival"}]
    rosters = [{"roster_id": 1, "owner_id": "u1", "settings": {"fpts": 500}},
               {"roster_id": 2, "owner_id": "u2", "settings": {"fpts": 400}}]
    players = {pid: {"full_name": pid.upper(), "position": next(iter(p))} for pid, p in POS.items()}
    monkeypatch.setattr(sleeper, "nfl_state", lambda: {"season": "2026", "week": 5})
    monkeypatch.setattr(sleeper, "find_my_team", lambda *a: (lg, users, rosters, rosters[0], users[0]))
    monkeypatch.setattr(sleeper, "all_players", lambda: players)
    monkeypatch.setattr(sleeper, "transactions", lambda lid, w: txns.get(w, []))
    monkeypatch.setattr(sleeper, "stats", lambda s, w: {pid: {"rush_yd": v * 10} for pid, v in weeks[w].points.items()})
    monkeypatch.setattr(sleeper, "matchups", lambda lid, w, **kw: [
        {"roster_id": r, "players": sorted(ps), "players_points": {p: weeks[w].points[p] for p in ps}}
        for r, ps in weeks[w].rosters.items()])

    md, csv = ledger.run("brendan", "Brendobendo", None, None, {"eval": {"max_adds": 3}}, tmp_path)
    text = md.read_text()
    assert "Net value of my moves: +23.0" in text            # +25 (t1) - 2 (t2)
    assert "FA_RB" in text and "max_adds" in text
    assert csv.exists()
