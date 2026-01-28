"""
Script: find the currently active 15-minute Bitcoin or Ethereum market on Polymarket.
Uses aiohttp to query the Gamma public-search endpoint, filters to markets ending
within 20 minutes, and returns condition_id, token_id_yes/no, and end_time.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp

GAMMA_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
ET = ZoneInfo("America/New_York")
WINDOW_MINUTES = 15
MAX_MINUTES_UNTIL_END = 20


def current_et() -> datetime:
    return datetime.now(ET)


def current_15m_window() -> tuple[datetime, str]:
    now = current_et()
    minute_floor = now.minute - (now.minute % WINDOW_MINUTES)
    start = now.replace(minute=minute_floor, second=0, microsecond=0)
    end = start + timedelta(minutes=WINDOW_MINUTES)
    start_str = f"{start.hour % 12 or 12}:{start.minute:02d}{'am' if start.hour < 12 else 'pm'}"
    end_str = f"{end.hour % 12 or 12}:{end.minute:02d}{'am' if end.hour < 12 else 'pm'}"
    return start, f"{start_str}-{end_str}"


async def search_gamma(session: aiohttp.ClientSession, query: str) -> dict:
    async with session.get(GAMMA_SEARCH_URL, params={"q": query}) as resp:
        resp.raise_for_status()
        return await resp.json()


def parse_token_ids(field) -> tuple[str, str]:
    try:
        if isinstance(field, list):
            ids = field
        else:
            ids = json.loads(field) if isinstance(field, str) else []
    except Exception:
        ids = []
    if len(ids) >= 2:
        return ids[0], ids[1]
    return "", ""


def extract_market(event: dict, market: dict) -> dict | None:
    cond = market.get("conditionId") or market.get("condition_id")
    end_raw = market.get("endDate") or market.get("umaEndDate") or event.get("endDate")
    if not cond or not end_raw:
        return None
    try:
        if isinstance(end_raw, (int, float)):
            end_dt = datetime.fromtimestamp(
                end_raw / 1000.0 if end_raw > 1e12 else end_raw, tz=timezone.utc
            )
        else:
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
    except Exception:
        return None
    yes, no = parse_token_ids(market.get("clobTokenIds") or [])
    return {
        "condition_id": cond,
        "token_id_yes": yes,
        "token_id_no": no,
        "end_time": end_dt,
        "title": (event.get("title") or "") + " " + (market.get("question") or ""),
        "custom_liveness": market.get("customLiveness") or 0,
        "closed": market.get("closed", False),
    }


async def find_active_15m_market() -> dict | None:
    now_et = current_et()
    now_utc = now_et.astimezone(timezone.utc)
    window_start, window_label = current_15m_window()
    window_hint = f"{window_start.hour % 12 or 12}:{window_start.minute:02d}"

    queries = ["Bitcoin Up or Down", "Ethereum Up or Down"]
    candidates = []

    async with aiohttp.ClientSession() as session:
        for q in queries:
            data = await search_gamma(session, q)
            for event in data.get("events") or []:
                title_lower = (event.get("title") or "").lower()
                if "up or down" not in title_lower:
                    continue
                is_btc = "bitcoin" in title_lower
                is_eth = "ethereum" in title_lower
                if not (is_btc or is_eth):
                    continue

                for market in event.get("markets") or []:
                    info = extract_market(event, market)
                    if not info or info["closed"]:
                        continue
                    end_dt = info["end_time"]
                    mins_left = (end_dt - now_utc).total_seconds() / 60
                    if mins_left < 0 or mins_left > MAX_MINUTES_UNTIL_END:
                        continue
                    title_lower_full = info["title"].lower()
                    is_15m = info["custom_liveness"] == 900 or (
                        "15" in title_lower_full and ("min" in title_lower_full or "minute" in title_lower_full)
                    )
                    if not is_15m:
                        continue
                    # Prefer markets whose title mentions the current window hint
                    title_matches_window = window_hint in title_lower_full or window_label.lower() in title_lower_full
                    candidates.append(
                        {
                            "condition_id": info["condition_id"],
                            "token_id_yes": info["token_id_yes"],
                            "token_id_no": info["token_id_no"],
                            "end_time": end_dt.isoformat(),
                            "minutes_until_end": round(mins_left, 2),
                            "title_matches_window": title_matches_window,
                        }
                    )

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            not x["title_matches_window"],
            x["minutes_until_end"],
        )
    )
    best = candidates[0]
    return {
        "condition_id": best["condition_id"],
        "token_id_yes": best["token_id_yes"],
        "token_id_no": best["token_id_no"],
        "end_time": best["end_time"],
    }


async def main() -> None:
    result = await find_active_15m_market()
    if result:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps({"error": "No active 15-minute BTC/ETH market ending in <20 minutes found"}))


if __name__ == "__main__":
    asyncio.run(main())
