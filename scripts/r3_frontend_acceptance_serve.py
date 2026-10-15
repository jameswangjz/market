"""Transient current-dist preview; proxy only anonymous storefront GET requests."""
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/'):
            if path not in ('/api/storefront/products', '/api/storefront/categories'):
                self.send_error(403)
                return
            try:
                with urlopen('http://192.168.10.10:30080' + self.path, timeout=15) as response:
                    body = response.read()
                    self.send_response(response.status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            except HTTPError as error:
                self.send_error(error.code)
            except OSError:
                self.send_error(502)
            return
        if path != '/' and not path.startswith('/assets/') and path != '/favicon.ico':
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self):
        self.send_error(403)

    def log_message(self, *_args):
        pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=4183)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parents[1] / 'frontend' / 'dist'
    server = ThreadingHTTPServer(('0.0.0.0', args.port), lambda *a, **kw: Handler(*a, directory=str(directory), **kw))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
