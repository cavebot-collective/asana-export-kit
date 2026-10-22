"""OAuth authorization-code + PKCE login; credentials live in memory for one run."""
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from .core import NoRedirect

AUTHORIZE = "https://app.asana.com/-/oauth_authorize"
TOKEN = "https://app.asana.com/-/oauth_token"


def pkce(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def callback_code(path, state, callback_path="/callback"):
    parsed = urllib.parse.urlsplit(path)
    if parsed.path != callback_path:
        raise ValueError("Unexpected callback path")
    q = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if len(q.get("state", [])) != 1 or not hmac.compare_digest(q["state"][0], state):
        raise ValueError("OAuth state mismatch")
    if "error" in q:
        raise PermissionError("Asana authorization was denied")
    if len(q.get("code", [])) != 1 or not q["code"][0]:
        raise ValueError("Missing or ambiguous OAuth authorization code")
    return q["code"][0]


class OAuthSession:
    def __init__(self, client_id, client_secret, redirect_uri, opener=None, clock=time.monotonic):
        if not client_id or not client_secret:
            raise ValueError("OAuth requires the registered app's client ID and client secret")
        self.client_id, self.client_secret, self.redirect_uri = client_id, client_secret, redirect_uri
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.clock, self.expires_at, self.access_token, self.refresh_token = clock, 0, None, None

    def exchange(self, fields):
        body = {"client_id": self.client_id, "client_secret": self.client_secret, **fields}
        request = urllib.request.Request(TOKEN, data=urllib.parse.urlencode(body).encode(), method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=60) as response:
                data = json.load(response)
        except (urllib.error.URLError, TimeoutError, ValueError):
            # Do not put response bodies/codes/credentials into logs or archives.
            raise ValueError("OAuth exchange failed; check app settings and authorization") from None
        if not data.get("access_token") or float(data.get("expires_in", 0)) <= 0:
            raise ValueError("OAuth response lacks a valid access token/expiry")
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.expires_at = self.clock() + float(data["expires_in"])

    def token(self):
        if self.clock() >= self.expires_at - 60:
            if not self.refresh_token:
                raise ValueError("OAuth refresh token unavailable; sign in again")
            self.exchange({"grant_type": "refresh_token", "refresh_token": self.refresh_token})
        return self.access_token


def login(client_id, client_secret, scopes, redirect_uri="http://127.0.0.1:8765/callback", timeout=300, open_browser=True):
    uri = urllib.parse.urlsplit(redirect_uri)
    if (uri.scheme != "http" or uri.hostname not in ("127.0.0.1", "localhost") or not uri.port
            or uri.username or uri.password or uri.query or uri.fragment or not uri.path):
        raise ValueError("This CLI requires a registered HTTP loopback redirect with explicit port/path")
    # Require an explicit grant rather than silently falling back to Full permissions.
    requested = scopes.split()
    if not requested or any(not scope.endswith(":read") for scope in requested):
        raise ValueError("Provide the registered app's space-separated read scopes")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    session = OAuthSession(client_id, client_secret, redirect_uri)
    outcome = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # HTTP request lines include secret authorization codes.

        def do_GET(self):
            try:
                code = callback_code(self.path, state, uri.path)
            except PermissionError:
                outcome["error"] = "Asana authorization was denied"
                self.send_response(403)
                message = b"Authorization declined. Return to the terminal."
            except ValueError:
                self.send_response(400)
                message = b"Invalid callback. Continue with the original authorization request."
            else:
                outcome["code"] = code
                self.send_response(200)
                message = b"Authorization received. You can close this tab."
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

    # Bind before opening the browser; fail if the registered port is occupied.
    with HTTPServer(("127.0.0.1", uri.port), Handler) as server:
        server.timeout = 0.5
        url = AUTHORIZE + "?" + urllib.parse.urlencode({"client_id": client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "state": state, "code_challenge_method": "S256", "code_challenge": pkce(verifier),
            "scope": " ".join(requested)})
        print("Authorize this exporter in your browser (URL contains no client secret):\n" + url, flush=True)
        if open_browser:
            webbrowser.open(url)
        deadline = time.monotonic() + timeout
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    if "error" in outcome:
        raise ValueError(outcome["error"])
    if "code" not in outcome:
        raise ValueError("OAuth login timed out")
    session.exchange({"grant_type": "authorization_code", "code": outcome["code"],
        "redirect_uri": redirect_uri, "code_verifier": verifier})
    return session
