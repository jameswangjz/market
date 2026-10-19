"""Basic SaaS entry path availability, not full query-dependent behavior or SSO."""
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException


def probe_saas_entry(ns, db, product):
    """Probe origin/path only, without credentials or enterprise/SSO flow context.

    The safe request helper enforces destination safety and bounds GET bodies.
    Query-dependent application behavior and full browser SSO are not tested.
    """
    message = 'SaaS应用访问入口检查未通过'
    raw = product.application_url or ''
    try:
        address = urlsplit(raw)
        if (address.scheme not in {'http', 'https'} or not address.hostname
                or address.username or address.password or address.fragment
                or address.port == 0 or '\\' in raw
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw)):
            raise ValueError('invalid_application_url')
        ns['application_address_policy']['validate_url'](raw, db=db)
    except (ValueError, TypeError):
        raise HTTPException(409, message) from None
    url = urlunsplit((address.scheme, address.netloc, address.path, '', ''))
    headers = {'X-Market-Product-Id': product.id}
    try:
        response = ns['validated_http_request']('HEAD', url, timeout=5,
            follow_redirects=False, headers=headers)
        if response.status_code in {405, 501}:
            response = ns['validated_http_request']('GET', url, timeout=5,
                follow_redirects=False, headers=headers)
    except Exception:
        raise HTTPException(502, message) from None
    if not 200 <= response.status_code < 400:
        raise HTTPException(502, f'{message} (HTTP {response.status_code})')
    return response.status_code
