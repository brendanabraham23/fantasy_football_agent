"""Game-day forecasts from Open-Meteo (free, no API key)."""
from __future__ import annotations

from datetime import date, datetime

from . import http

# Home stadium coordinates by nflverse team abbreviation.
STADIUMS = {
    "ARI": (33.5276, -112.2626), "ATL": (33.7554, -84.4008), "BAL": (39.2780, -76.6227),
    "BUF": (42.7738, -78.7870), "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167),
    "CIN": (39.0955, -84.5161), "CLE": (41.5061, -81.6995), "DAL": (32.7473, -97.0945),
    "DEN": (39.7439, -105.0201), "DET": (42.3400, -83.0456), "GB": (44.5013, -88.0622),
    "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639), "JAX": (30.3239, -81.6373),
    "KC": (39.0489, -94.4839), "LV": (36.0909, -115.1833), "LAC": (33.9535, -118.3392),
    "LA": (33.9535, -118.3392), "MIA": (25.9580, -80.2389), "MIN": (44.9737, -93.2575),
    "NE": (42.0909, -71.2643), "NO": (29.9511, -90.0812), "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745), "PHI": (39.9008, -75.1675), "PIT": (40.4468, -80.0158),
    "SF": (37.4030, -121.9700), "SEA": (47.5952, -122.3316), "TB": (27.9759, -82.5033),
    "TEN": (36.1665, -86.7713), "WAS": (38.9077, -76.8645),
}
INDOOR_ROOFS = {"dome", "closed"}
PASS_POS = {"QB", "WR", "TE"}


def forecast(game: dict) -> dict | None:
    """Average conditions over the ~3 hours after kickoff, or None if unavailable."""
    if not game:
        return None
    if str(game.get("roof", "")).lower() in INDOOR_ROOFS:
        return {"indoor": True, "summary": "Indoors"}
    if game.get("neutral"):
        return {"indoor": False, "summary": f"Neutral site ({game.get('stadium')}) - check manually",
                "unknown": True}
    coords = STADIUMS.get(game.get("home_team"))
    gameday = str(game.get("gameday"))
    if not coords:
        return None
    days_out = (date.fromisoformat(gameday) - date.today()).days
    if days_out < 0 or days_out > 15:
        return None

    data = http.get_json(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": coords[0], "longitude": coords[1],
            "hourly": "temperature_2m,precipitation,snowfall,wind_speed_10m,precipitation_probability",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch",
            "timezone": "America/New_York", "start_date": gameday, "end_date": gameday,
        },
        ttl=3 * 3600,
    )
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    kick_hour = int(str(game.get("gametime") or "13:00").split(":")[0])  # nflverse times are ET
    idx = [i for i, t in enumerate(times) if kick_hour <= datetime.fromisoformat(t).hour <= kick_hour + 3]
    if not idx:
        return None

    def avg(key):
        vals = [hourly[key][i] for i in idx if hourly.get(key) and hourly[key][i] is not None]
        return sum(vals) / len(vals) if vals else 0.0

    wx = {
        "indoor": False,
        "temp_f": round(avg("temperature_2m")),
        "wind_mph": round(avg("wind_speed_10m")),
        "precip_in_hr": round(avg("precipitation"), 2),
        "snow_in_hr": round(avg("snowfall"), 2),
        "precip_prob": round(avg("precipitation_probability")),
    }
    parts = [f"{wx['temp_f']}F", f"wind {wx['wind_mph']}mph"]
    if wx["snow_in_hr"] > 0:
        parts.append("snow")
    elif wx["precip_in_hr"] >= 0.02 or wx["precip_prob"] >= 60:
        parts.append(f"rain {wx['precip_prob']}%")
    wx["summary"] = ", ".join(parts)
    return wx


def multiplier(position: str, wx: dict | None) -> float:
    """Scoring adjustment for weather. Wind and heavy precip hurt passing and kicking."""
    if not wx or wx.get("indoor") or wx.get("unknown"):
        return 1.0
    m = 1.0
    wind = wx.get("wind_mph", 0)
    if wind > 15:
        steps = (wind - 15) / 5
        if position in PASS_POS:
            m -= 0.03 * steps
        elif position == "K":
            m -= 0.06 * steps
        elif position == "DEF":
            m += 0.02 * steps
    wet = wx.get("snow_in_hr", 0) > 0 or wx.get("precip_in_hr", 0) >= 0.1
    if wet:
        m += {"QB": -0.04, "WR": -0.04, "TE": -0.03, "K": -0.05, "RB": 0.02, "DEF": 0.04}.get(position, 0)
    if wx.get("temp_f", 60) < 20 and position in PASS_POS | {"K"}:
        m -= 0.02
    return max(0.75, min(1.1, m))
