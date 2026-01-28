#!/usr/bin/env python3
"""
High-frequency Polymarket sniper.

Behavior (per user spec):
- Streams Level 1 order book data from wss://ws-subscriptions-clob.polymarket.com/ws/market.
- Tracks the best ask for the current "winning" side (price > 0.5) for the provided token IDs.
- When time remaining is <= 1 second and > 0, and the winning side is askable below $0.99,
  it sends a Fill-or-Kill buy at $0.99 via py-clob-client (or logs "WOULD BUY" in dry-run mode).

Notes:
- The websocket message schema can vary; extraction helpers below try several common field names.
- Fill in your private key and adjust ClobClient construction for your deployment if needed.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

import websockets

# py-clob-client 0.34.x exports ClobClient from py_clob_client.client
try:
    from py_clob_client.client import ClobClient
except ImportError as exc:
    raise SystemExit(
        "py-clob-client is required. Install with `pip install py-clob-client`."
    ) from exc


WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class SniperConfig:
    condition_id: str
    token_ids: Iterable[str]
    max_price: float = 0.99
    dry_run: bool = True
    size: float = 1.0
    chain_id: int = 137  # Default to Polygon mainnet.
    api_url: str = "https://clob.polymarket.com"
    private_key: Optional[str] = field(default_factory=lambda: os.environ.get("POLYMARKET_PK"))


class Sniper:
    def __init__(self, cfg: SniperConfig, client: ClobClient) -> None:
        self.cfg = cfg
        self.client = client
        self.best_asks: Dict[str, float] = {}
        self.time_remaining: Optional[float] = None
        self.triggered: bool = False
        self.stop_event = asyncio.Event()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for signame in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signame, self.stop_event.set)

        while not self.stop_event.is_set():
            try:
                await self._stream_loop()
            except (websockets.ConnectionClosed, websockets.InvalidMessage) as exc:
                logging.warning("Websocket error (%s); reconnecting...", exc)
                await asyncio.sleep(0)  # Yield to allow stop_event to be processed.

    async def _stream_loop(self) -> None:
        async with websockets.connect(WS_MARKET_URL, ping_interval=20, ping_timeout=20) as ws:
            await self._subscribe(ws)
            async for raw in ws:
                if self.stop_event.is_set():
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logging.debug("Ignoring non-JSON payload: %s", raw)
                    continue

                if not self._is_target_condition(msg):
                    continue

                self._update_state(msg)
                await self._maybe_snipe()

    async def _subscribe(self, ws: websockets.WebSocketClientProtocol) -> None:
        """
        Subscribes to Level 1 data for the condition ID.
        Adjust payload if your feed expects a different shape.
        """
        subscribe_payload = {
            "type": "subscribe",
            "channel": "market",
            "conditionId": self.cfg.condition_id,
            "depth": 1,
        }
        await ws.send(json.dumps(subscribe_payload))
        logging.info("Subscribed to L1 for condition %s", self.cfg.condition_id)

    def _is_target_condition(self, msg: dict) -> bool:
        cond = (
            msg.get("conditionId")
            or msg.get("condition_id")
            or msg.get("conditionID")
            or msg.get("condition")
        )
        return cond is None or cond == self.cfg.condition_id

    def _extract_time_remaining(self, msg: dict) -> Optional[float]:
        for key in ("timeRemaining", "time_remaining", "secondsRemaining", "timeToMaturity", "t"):
            if key in msg:
                try:
                    return float(msg[key])
                except (TypeError, ValueError):
                    return None
        data = msg.get("data") or {}
        for key in ("timeRemaining", "time_remaining", "secondsRemaining"):
            if key in data:
                try:
                    return float(data[key])
                except (TypeError, ValueError):
                    return None
        return None

    def _extract_best_asks(self, msg: dict) -> Dict[str, float]:
        """
        Attempts to parse best ask prices from common shapes:
        - msg["book"][token_id]["asks"] -> list of price dicts/arrays
        - msg["book"][token_id]["bestAsk"] (or best_ask)
        - msg["bestAsk"] alongside msg["tokenId"]
        """
        bests: Dict[str, float] = {}
        book = msg.get("book") or (msg.get("data") or {}).get("book")
        if isinstance(book, dict):
            for token_id in self.cfg.token_ids:
                token_book = book.get(token_id) if isinstance(book.get(token_id), dict) else book.get(str(token_id))
                if isinstance(token_book, dict):
                    price = self._first_price(token_book.get("bestAsk") or token_book.get("best_ask"))
                    if price is None:
                        price = self._first_price(token_book.get("asks"))
                    if price is not None:
                        bests[str(token_id)] = price
        token_id = msg.get("tokenId") or msg.get("token_id")
        if token_id:
            price = self._first_price(msg.get("bestAsk") or msg.get("best_ask") or msg.get("ask"))
            if price is not None:
                bests[str(token_id)] = price
        return bests

    @staticmethod
    def _first_price(asks) -> Optional[float]:
        if not asks:
            return None
        first = asks[0]
        if isinstance(first, dict):
            for key in ("price", "px", "p"):
                if key in first:
                    try:
                        return float(first[key])
                    except (TypeError, ValueError):
                        return None
        if isinstance(first, (list, tuple)) and first:
            try:
                return float(first[0])
            except (TypeError, ValueError):
                return None
        try:
            return float(first)
        except (TypeError, ValueError):
            return None

    def _update_state(self, msg: dict) -> None:
        time_remaining = self._extract_time_remaining(msg)
        if time_remaining is not None:
            self.time_remaining = time_remaining
        bests = self._extract_best_asks(msg)
        if bests:
            self.best_asks.update(bests)
            logging.debug("Updated best asks: %s", self.best_asks)

    def _winning_side(self) -> Optional[tuple[str, float]]:
        winning = {tid: px for tid, px in self.best_asks.items() if px > 0.5}
        if not winning:
            return None
        token_id, price = max(winning.items(), key=lambda kv: kv[1])
        return token_id, price

    async def _maybe_snipe(self) -> None:
        if self.triggered:
            return
        if self.time_remaining is None or not (0 < self.time_remaining <= 1):
            return
        winning = self._winning_side()
        if not winning:
            return
        token_id, best_ask = winning
        if best_ask >= self.cfg.max_price:
            return

        self.triggered = True
        logging.info(
            "Trigger hit: time_remaining=%.4f, token=%s, best_ask=%.4f < max_price=%.4f",
            self.time_remaining,
            token_id,
            best_ask,
            self.cfg.max_price,
        )

        if self.cfg.dry_run:
            print("WOULD BUY", token_id, "@", self.cfg.max_price)
            return

        await self._execute_order(token_id)

    async def _execute_order(self, token_id: str) -> None:
        order_params = {
            "token_id": token_id,
            "price": self.cfg.max_price,
            "size": self.cfg.size,
            "side": "BUY",
            "time_in_force": "FOK",
        }
        logging.info("Submitting FOK buy: %s", order_params)
        try:
            result = self.client.create_and_post_order(**order_params)
            logging.info("Order result: %s", result)
        except Exception:
            logging.exception("Failed to submit order")


def build_client(cfg: SniperConfig) -> ClobClient:
    """
    Construct a py-clob-client instance. Adjust if your version expects
    different constructor args.
    """
    if not cfg.private_key:
        raise SystemExit("Private key missing. Set --private-key or POLYMARKET_PK env var.")

    # ClobClient signature for py-clob-client>=0.34: host, key, chain_id
    return ClobClient(host=cfg.api_url, chain_id=cfg.chain_id, key=cfg.private_key)


def parse_args() -> SniperConfig:
    parser = argparse.ArgumentParser(description="Polymarket Level 1 sniper.")
    parser.add_argument("--condition-id", required=True, help="Target condition ID.")
    parser.add_argument(
        "--token-id",
        action="append",
        dest="token_ids",
        required=True,
        help="Token ID to watch (repeatable).",
    )
    parser.add_argument("--size", type=float, default=1.0, help="Order size in outcome tokens.")
    parser.add_argument("--max-price", type=float, default=0.99, help="Max price to pay (FOK).")
    parser.add_argument("--dry-run", action="store_true", help="If set, only log 'WOULD BUY'.")
    parser.add_argument(
        "--private-key",
        default=None,
        help="Private key for signing orders (or set POLYMARKET_PK).",
    )
    parser.add_argument(
        "--api-url", default="https://clob.polymarket.com", help="py-clob-client API base URL."
    )
    parser.add_argument("--chain-id", type=int, default=137, help="Chain ID (Polygon mainnet=137).")

    args = parser.parse_args()
    return SniperConfig(
        condition_id=args.condition_id,
        token_ids=args.token_ids,
        max_price=args.max_price,
        dry_run=args.dry_run,
        size=args.size,
        private_key=args.private_key or os.environ.get("POLYMARKET_PK"),
        api_url=args.api_url,
        chain_id=args.chain_id,
    )


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    cfg = parse_args()
    client = build_client(cfg)
    sniper = Sniper(cfg, client)
    await sniper.run()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()

