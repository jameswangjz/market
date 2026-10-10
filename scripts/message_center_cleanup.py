"""One-shot retention cleanup using the API's database configuration."""
import importlib
import logging
import os


def main():
    os.environ['MESSAGE_CENTER_DISABLE_BACKGROUND'] = 'true'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    center = importlib.import_module('app.main').message_center
    logging.info('Notification retention cleanup: %s', center['cleanup']())


if __name__ == '__main__':
    main()
