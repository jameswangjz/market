"""One-shot command entry point for the application health CronJob."""
import logging


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    from .main import SessionLocal, application_health
    with SessionLocal() as db:
        processed = application_health['run_once'](db)
    logging.info('Application health sweep processed %d targets', processed)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
