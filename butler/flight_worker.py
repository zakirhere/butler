import sys

from butler.flights import run_once, send_weekly_summary


if __name__ == "__main__":
    if "--weekly-summary" in sys.argv:
        send_weekly_summary(force="--force" in sys.argv)
    else:
        run_once()
