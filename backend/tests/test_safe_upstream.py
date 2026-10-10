"""Offline SSRF tests. Run with unittest discover; no pytest or app.main."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import ssl
import sys
import unittest
from unittest.mock import patch

import httpcore
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import safe_upstream as upstream


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks=(b"ok",)):
        self.chunks, self.reads, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class SafeUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "UPSTREAM_ALLOWED_HOSTS": "", "UPSTREAM_ALLOWED_CIDRS": "",
        }))
        self.stack.enter_context(patch.object(socket, "getaddrinfo",
            side_effect=AssertionError("Real DNS forbidden")))
        self.stack.enter_context(patch.object(socket.socket, "connect",
            side_effect=AssertionError("Real sockets forbidden")))
        self.addresses, self.dns_calls = ["93.184.216.34"], []
        self.requests, self.transport_options = [], []
        self.stream, self.status, self.response_headers = Stream(), 200, {}

        async def resolve(loop, host, port, **kwargs):
            self.dns_calls.append((host, port, kwargs))
            return [(socket.AF_INET6 if ":" in address else socket.AF_INET,
                     socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))
                    for address in self.addresses]

        async def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status, headers=self.response_headers, stream=self.stream)

        def transport(**kwargs):
            self.transport_options.append(kwargs)
            return httpx.MockTransport(handle)

        self.stack.enter_context(patch.object(asyncio.BaseEventLoop, "getaddrinfo", resolve))
        self.real_transport = httpx.AsyncHTTPTransport
        self.stack.enter_context(patch.object(httpx, "AsyncHTTPTransport", transport))

    def probe(self, url="https://health.example/health", **kwargs):
        return upstream.validated_http_request(**({"method": "GET", "url": url, "timeout": 1} | kwargs))

    def test_unsafe_url_shapes_without_dns(self):
        for url in (
            "ftp://health.example/", "//health.example/", "https:///health", "https://",
            "https://user@health.example/", "https://user:pass@health.example/",
            "https://health.example/?", "https://health.example/?a=1", "https://health.example/#",
            "https://health.example/#fragment", "https://health.example\\@127.0.0.1/",
            "https://health.example/\nhealth", " https://health.example/",
            "https://health.example:0/", "https://health.example:65536/", "https://health.example:/",
            "https://health.example/%2f%2fattacker.example", "https://health.example/%5cfoo",
            "https://health.example/%252f%252fattacker.example", "https://health.example/%253furl=x",
            "https://health.example/%23fragment", "https://health.example/%40evil",
            "https://health.example/%00", "https://health.example/%0d%0aHost:evil",
            "https://health.example/%2500", "https://health.example/%invalid",
            "https://health.example/%FF", "https://%31%32%37.0.0.1/",
            "https://[fe80::1%25eth0]/", "https://*.example/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                upstream.validate_upstream_url(url)
        self.assertEqual(self.dns_calls, [])

    def test_shape_helper_does_not_authorize_or_resolve(self):
        for url, host in (
            ("https://Health.Example./health", "health.example"),
            ("http://service.namespace.svc.cluster.local:8080/ready", "service.namespace.svc.cluster.local"),
            ("https://[2606:4700:4700::1111]/health", "2606:4700:4700::1111"),
            ("https://health.example/%68ealth", "health.example"),
            ("https://10.0.0.1/health", "10.0.0.1"),
        ):
            with self.subTest(url=url):
                self.assertEqual(upstream.validate_upstream_url(url).host, host)
        self.assertEqual(self.dns_calls, [])

    def test_only_health_methods_and_finite_timeouts(self):
        for method in ("POST", "PUT", "DELETE", "OPTIONS", "TRACE", " GET", None):
            with self.subTest(method=method), self.assertRaises(ValueError):
                self.probe(method=method)
        for timeout in (0, -1, 0.01, float("inf"), float("nan"), True, "1", None):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.probe(timeout=timeout)
        self.assertEqual(self.dns_calls, [])
        self.assertEqual(self.requests, [])
        self.probe(timeout=30)
        self.assertTrue(all(value == 10 for value in self.requests[0].extensions["timeout"].values()))

    def test_redirect_responses_are_never_followed(self):
        with self.assertRaises(ValueError):
            self.probe(follow_redirects=True)
        self.response_headers = {"Location": "http://169.254.169.254/latest/meta-data/"}
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.status, self.stream = status, Stream()
                response = self.probe()
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.extensions["validated_upstream_addresses"], ("93.184.216.34",))
        self.assertEqual(len(self.dns_calls), 5)
        self.assertEqual(len(self.requests), 5)

    def test_hard_denials_override_allowlists(self):
        os.environ.update(UPSTREAM_ALLOWED_HOSTS="health.example", UPSTREAM_ALLOWED_CIDRS="0.0.0.0/0,::/0")
        for address in (
            "127.0.0.1", "0.0.0.0", "169.254.169.254", "169.254.170.2", "224.0.0.1",
            "255.255.255.255", "192.0.2.1", "168.63.129.16", "100.100.100.200",
            "::1", "::", "fe80::1", "fec0::1", "ff02::1", "fd00:ec2::254", "fd20:ce::254",
            "::ffff:127.0.0.1", "::ffff:169.254.169.254", "2002:7f00:1::1",
            "64:ff9b::7f00:1", "64:ff9b:1::a00:1",
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.addresses = [address]
                self.probe()
        self.assertEqual(self.requests, [])

    def test_private_requires_explicit_authorization(self):
        for address in ("10.1.2.3", "172.16.1.1", "192.168.1.1", "100.64.0.1", "fd12::1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                self.addresses = [address]
                self.probe()
        self.assertEqual(self.requests, [])

    def test_kubernetes_exact_hostname_authorizes_private(self):
        host = "service.namespace.svc.cluster.local"
        os.environ["UPSTREAM_ALLOWED_HOSTS"] = f" {host.upper()},another.example "
        self.addresses = ["10.43.12.9", "fd12::2"]
        response = self.probe(f"https://{host}:8443/health")
        request = self.requests[0]
        self.assertEqual(request.url.host, "10.43.12.9")
        self.assertEqual(request.headers["host"], f"{host}:8443")
        self.assertEqual(request.extensions["sni_hostname"], host)
        self.assertEqual(response.extensions["validated_upstream_addresses"], ("10.43.12.9", "fd12::2"))
        self.assertEqual(response.extensions["validated_upstream_ip"], "10.43.12.9")

    def test_host_allowlist_is_exact(self):
        os.environ["UPSTREAM_ALLOWED_HOSTS"] = "health.example"
        self.addresses = ["10.1.2.3"]
        for host in ("sub.health.example", "health.example.attacker.example"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.probe(f"https://{host}/")
        self.assertEqual(self.requests, [])

    def test_invalid_allowlists_fail_closed(self):
        for value in ("*.example", "https://health.example", "health.example:443"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                os.environ["UPSTREAM_ALLOWED_HOSTS"] = value
                self.probe()
        os.environ["UPSTREAM_ALLOWED_HOSTS"] = ""
        os.environ["UPSTREAM_ALLOWED_CIDRS"] = "10.1.1.1/8"
        with self.assertRaises(ValueError):
            self.probe()
        self.assertEqual(self.requests, [])

    def test_cidr_authorizes_only_its_private_range(self):
        os.environ["UPSTREAM_ALLOWED_CIDRS"] = "10.43.0.0/16"
        self.addresses = ["10.43.1.2"]
        self.assertEqual(self.probe().extensions["validated_upstream_ip"], "10.43.1.2")
        self.addresses = ["10.44.1.2"]
        with self.assertRaises(ValueError):
            self.probe()
        self.assertEqual(len(self.requests), 1)

    def test_every_dns_answer_must_pass(self):
        for addresses in (
            ["93.184.216.34", "10.0.0.1"], ["10.0.0.1", "93.184.216.34"],
            ["93.184.216.34", "::1"], ["2606:4700:4700::1111", "169.254.169.254"], [],
        ):
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                self.addresses = addresses
                self.probe()
        self.assertEqual(self.requests, [])

    def test_public_targets_pin_and_expose_all_addresses(self):
        self.addresses = ["2606:4700:4700::1111", "93.184.216.34", "93.184.216.34"]
        os.environ.update(HTTPS_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9",
                          SSL_CERT_FILE="/nonexistent/attacker-ca.pem")
        response = self.probe(headers={"Host": "attacker.example", "Accept-Encoding": "gzip", "X-Probe": "yes"})
        request = self.requests[0]
        self.assertEqual(request.url.host, "2606:4700:4700::1111")
        self.assertEqual(request.url.raw_path, b"/health")
        self.assertEqual(request.headers["Host"], "health.example")
        self.assertEqual(request.headers["Accept-Encoding"], "identity")
        self.assertEqual(request.headers["X-Probe"], "yes")
        self.assertEqual(request.extensions["sni_hostname"], "health.example")
        self.assertEqual(self.transport_options, [{"trust_env": False, "retries": 0}])
        self.assertEqual(response.url, httpx.URL("https://health.example/health"))
        self.assertEqual(response.content, b"ok")
        self.assertTrue(response.is_closed and self.stream.closed)
        self.assertEqual(response.extensions["validated_upstream_addresses"], ("2606:4700:4700::1111", "93.184.216.34"))
        self.assertEqual(len(self.dns_calls), 1)

    def test_ip_literals_skip_dns(self):
        for url, host in (
            ("http://93.184.216.34:8080/health", "93.184.216.34:8080"),
            ("https://[2606:4700:4700::1111]/health", "[2606:4700:4700::1111]"),
        ):
            with self.subTest(url=url):
                self.stream = Stream()
                response = self.probe(url)
                self.assertEqual(self.requests[-1].headers["host"], host)
                self.assertEqual(response.extensions["validated_upstream_ip"], httpx.URL(url).host)
        self.assertEqual(self.dns_calls, [])

    def test_head_never_reads_body(self):
        response = self.probe(method="head")
        self.assertEqual(response.content, b"")
        self.assertEqual(self.stream.reads, 0)
        self.assertTrue(self.stream.closed)
        self.assertEqual(self.requests[0].method, "HEAD")

    def test_get_reads_bounded_prefix_and_closes(self):
        self.stream = Stream([b"x" * 4097, b"never read"])
        self.response_headers = {"Content-Length": "999999999", "Transfer-Encoding": "chunked"}
        response = self.probe()
        self.assertEqual(response.content, b"x" * upstream.MAX_RESPONSE_BYTES)
        self.assertTrue(response.extensions["upstream_body_truncated"])
        self.assertEqual(response.headers["Content-Length"], "4096")
        self.assertNotIn("Transfer-Encoding", response.headers)
        self.assertEqual(self.stream.reads, 1)
        self.assertTrue(self.stream.closed)

    def test_small_chunks_accumulate_up_to_limit(self):
        self.stream = Stream([b"a", b"b", b"c"])
        response = self.probe()
        self.assertEqual(response.content, b"abc")
        self.assertFalse(response.extensions["upstream_body_truncated"])

    def test_compression_cannot_expand_response_bound(self):
        self.response_headers = {"Content-Encoding": "gzip"}
        with self.assertRaises(httpx.DecodingError):
            self.probe()
        self.assertEqual(self.stream.reads, 0)
        self.assertTrue(self.stream.closed)

    def test_request_framing_and_controls_rejected_before_connect(self):
        for headers in ({"Content-Length": "1"}, {"Transfer-Encoding": "chunked"},
                        {"Upgrade": "websocket"}, {"X-Probe": "ok\r\nHost: evil"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.probe(headers=headers)
        self.assertEqual(self.dns_calls, [])
        self.assertEqual(self.requests, [])

    def test_dns_failure_is_connect_error(self):
        async def fail(*args, **kwargs):
            raise socket.gaierror("not found")

        with patch.object(asyncio.BaseEventLoop, "getaddrinfo", fail), self.assertRaises(httpx.ConnectError):
            self.probe()
        self.assertEqual(self.requests, [])

    def test_dns_is_in_overall_deadline(self):
        async def hang(*args, **kwargs):
            await asyncio.Future()

        with patch.object(asyncio.BaseEventLoop, "getaddrinfo", hang), self.assertRaises(httpx.TimeoutException):
            self.probe(timeout=0.1)
        self.assertEqual(self.requests, [])

    def test_slow_body_is_in_overall_deadline(self):
        class SlowStream(Stream):
            async def __aiter__(self):
                yield b"a"
                await asyncio.Future()

        self.stream = SlowStream()
        with self.assertRaises(httpx.TimeoutException):
            self.probe(timeout=0.1)
        self.assertTrue(self.stream.closed)

    def test_real_transport_pins_socket_and_preserves_tls_name(self):
        calls = {"tcp": [], "tls": [], "writes": [], "closed": False}

        class NetworkStream(httpcore.AsyncNetworkStream):
            async def read(self, max_bytes, timeout=None):
                return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"

            async def write(self, buffer, timeout=None):
                calls["writes"].append(buffer)

            async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                calls["tls"].append((server_hostname, ssl_context.check_hostname, ssl_context.verify_mode))
                return self

            async def aclose(self):
                calls["closed"] = True

            def get_extra_info(self, info):
                return None

        async def connect(backend, host, port, **kwargs):
            calls["tcp"].append((host, port))
            # A second lookup would now find metadata; only the numeric IP
            # validated before this simulated rebind may reach the transport.
            self.addresses = ["169.254.169.254"]
            return NetworkStream()

        with patch.object(httpx, "AsyncHTTPTransport", self.real_transport), \
                patch.object(httpcore._backends.auto.AutoBackend, "connect_tcp", connect):
            os.environ.update(HTTPS_PROXY="http://127.0.0.1:9", SSL_CERT_FILE="/nonexistent/attacker-ca.pem")
            response = self.probe("https://health.example:8443/health")
        self.assertEqual(response.content, b"ok")
        self.assertEqual(calls["tcp"], [("93.184.216.34", 8443)])
        self.assertEqual(calls["tls"], [("health.example", True, ssl.CERT_REQUIRED)])
        self.assertIn(b"Host: health.example:8443\r\n", b"".join(calls["writes"]))
        self.assertEqual(len(self.dns_calls), 1)
        self.assertTrue(calls["closed"])
        self.assertEqual(response.extensions["validated_upstream_ip"], "93.184.216.34")


if __name__ == "__main__":
    unittest.main()
