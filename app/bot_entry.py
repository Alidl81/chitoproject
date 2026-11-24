import asyncio
import logging
import sys

from app.bot.runtime import run_bot

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    platform = sys.argv[1] if len(sys.argv) > 1 else ""
    if platform not in {"telegram", "bale"}:
        raise SystemExit("usage: python -m app.bot_entry telegram|bale")
    asyncio.run(run_bot(platform))
