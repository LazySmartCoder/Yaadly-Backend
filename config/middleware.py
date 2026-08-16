import ipaddress

from django.http import HttpResponsePermanentRedirect


def _is_lan_host(host):
    """True when the Host header points at a loopback or private-network address."""
    hostname = host.rsplit(":", 1)[0].strip("[]")
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_private or ipaddress.ip_address(
            hostname
        ).is_loopback
    except ValueError:
        # Not an IP literal; treat as a public hostname and redirect.
        return False


class LanAwareSSLRedirectMiddleware:
    """Redirect HTTP -> HTTPS for public hosts only.

    Private/LAN addresses (e.g. a physical phone hitting the dev server on
    192.168.x.x) stay on plain HTTP so they can reach `runserver` without a
    TLS cert. Used alongside `SECURE_SSL_REDIRECT=False`; SecurityMiddleware
    would otherwise force HTTPS for every request when DEBUG is off.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.is_secure() and not _is_lan_host(request.get_host()):
            return HttpResponsePermanentRedirect(
                f"https://{request.get_host()}{request.get_full_path()}"
            )
        return self.get_response(request)
