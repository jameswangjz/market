"""Daily current/next-year holiday sync using the API's runtime and database."""
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def main():
    from app.holiday_calendar import main as run
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
