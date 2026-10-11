"""Registration metadata, not an OIDC authorization-server implementation."""
from urllib.parse import urlsplit


def validate_redirect_uris(values):
    result = []
    for value in values:
        if len(value) > 2000 or '\\' in value or any(ch.isspace() or ord(ch) < 32 for ch in value):
            raise ValueError("OIDC回调地址不能含空白或控制字符")
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("OIDC回调地址端口无效") from None
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.fragment or "#" in value or "*" in value
                or parsed.scheme not in {"https", "http"} or not parsed.path.startswith("/")
                or (port is not None and port == 0)):
            raise ValueError("OIDC回调地址须为完整URL，不允许凭据、片段或通配符")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "market-mock-saas"}:
            raise ValueError("OIDC回调地址须使用HTTPS，仅允许明确的本地模拟地址使用HTTP")
        if value not in result:
            result.append(value)
    return result


def pop_registration_config(values):
    import json
    values["oidc_redirect_uris_json"] = json.dumps(values.pop("oidc_redirect_uris", []))
