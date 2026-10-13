"""Run one idempotent application integration health sweep."""
import sys
import os
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def main():
    os.environ.setdefault('MESSAGE_CENTER_DISABLE_BACKGROUND', 'true')
    from app.application_health_worker import main as run
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
