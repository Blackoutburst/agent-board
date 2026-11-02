"""Verify shared sessions over loopback; never trust browser identity headers."""
import json
import os
import urllib.request

SHARED_AUTH = os.environ.get("BLACKOUT_SHARED_AUTH", "0") == "1"


def platform_user(cookie, admin=True):
    if not cookie:
        return None
    request = urllib.request.Request("http://127.0.0.1:9011/internal/account/session", headers={"Cookie": cookie})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                return None
            user = json.loads(response.read(8192)).get("user")
        if not isinstance(user, dict) or not user.get("id") or not user.get("username"):
            return None
        if user.get("role") not in ({"admin"} if admin else {"admin", "user"}):
            return None
        return user
    except (OSError, ValueError, TypeError):
        return None


def platform_logout(cookie):
    request = urllib.request.Request('http://127.0.0.1:9011/api/account/logout', data=b'{}', method='POST',
        headers={'Cookie': cookie, 'Origin': 'https://blackoutburst.com', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=3) as response:
        if response.status != 200:
            raise OSError('Shared logout failed')
        return response.headers.get('Set-Cookie', '')
