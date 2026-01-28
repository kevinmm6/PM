"""
Approve USDC for trading on Polymarket using py-clob-client.
Loads PRIVATE_KEY from .env, initializes ClobClient on Polygon mainnet (chain 137),
and calls client.set_allowance to authorize spending.
"""

import os

from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

CHAIN_ID = 137
HOST = "https://clob.polymarket.com"


def main() -> None:
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("Error: PRIVATE_KEY not set in .env")
        return

    client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=private_key)

    try:
        client.set_allowance()
        print("Success")
    except Exception as e:
        print(getattr(e, "message", str(e)))


if __name__ == "__main__":
    main()
