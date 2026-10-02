"""Desktop Auth Helper — the human-in-the-loop bridge for Cairn authentication.

This package runs on the operator's desktop (not inside the Docker dispatcher). It polls
the Cairn server for pending ``AuthRequest`` records, claims them atomically, notifies
the user, and — when ``auto_launch`` is enabled — opens a headed Chromium so the human
can complete password / SSO / MFA / CAPTCHA / hardware-key challenges. The browser must
run on the real user desktop because MFA, hardware keys, SSO and proxy/VPN/certificate
setup only exist there.

The helper never handles secrets itself; it only drives the *flow* and lets the existing
``cairn auth login`` command capture and verify the session.
"""

from cairn.auth_helper.client import AuthHelperClient
from cairn.auth_helper.daemon import AuthHelperDaemon
from cairn.auth_helper.desktop import DesktopNotifier
from cairn.auth_helper.launcher import AuthLoginLauncher

__all__ = [
    "AuthHelperClient",
    "AuthHelperDaemon",
    "DesktopNotifier",
    "AuthLoginLauncher",
]
