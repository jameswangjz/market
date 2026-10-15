"""Small anonymous deployed-contract probe; never authenticates or writes business data."""
import argparse
import json
from urllib.error import HTTPError
from urllib.request import urlopen


def probe(base):
    results = []
    for path in ('/api/development/tasks', '/api/applications'):
        try:
            with urlopen(base + path, timeout=15) as response:
                status = response.status
        except HTTPError as error:
            status = error.code
        results.append({'path': path, 'status': status, 'passed': status in (401, 403)})
    for size in (10, 20, 50, 100, 12):
        path = f'/api/storefront/products?page=1&page_size={size}'
        with urlopen(base + path, timeout=15) as response:
            data = json.load(response)
        results.append({'path': path, 'page_size': data.get('page_size'), 'total': data.get('total'),
                        'passed': data.get('page_size') == size and data.get('page') == 1 and len(data.get('items', [])) <= size})
    return {'mode': 'anonymous GET only', 'checks': results, 'passed': all(item['passed'] for item in results),
            'limits': ['No authenticated-role proof', 'No business mutations', 'No task status changes']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://192.168.10.10:30080')
    args = parser.parse_args()
    result = probe(args.base)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
