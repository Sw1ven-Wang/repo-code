"""Host-only Sign in with ChatGPT (public OAuth client, never Codex token scraping)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
import urllib.request

from .util import RepoError, atomic_write, execution_lock, private_dir

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
TOKEN_ENDPOINT = ISSUER + '/api/accounts/oauth/token'
AUTHORIZE_ENDPOINT = ISSUER + '/api/accounts/authorize'
PLAN_SCOPE = 'chatgpt.tokens.use.direct'
SCOPES = 'openid profile email offline_access resource.invoke ' + PLAN_SCOPE


def chatgpt_proxy():
    """Use the user's existing route; never change OS settings or log proxy URLs.

    httpx does not read Windows Internet Settings. Also, urllib.getproxies()
    can miss those settings when the environment contains NO_PROXY alone.
    Explicit standard proxy environment variables retain their native behavior.
    """
    explicit = os.getenv('REPO_CODE_CHATGPT_PROXY')
    if explicit is not None:
        if explicit == 'direct':
            return None
        proxy = explicit
    elif any(os.getenv(key) for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY',
                                        'https_proxy', 'http_proxy', 'all_proxy')):
        return None
    elif os.name == 'nt':
        proxies = urllib.request.getproxies_registry()
        proxy = proxies.get('https') or proxies.get('http')
    else:
        return None
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    try:
        valid = parsed.scheme in {'http', 'https'} and parsed.hostname and parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise RepoError('CHATGPT_INVALID_PROXY', 'Use an HTTP/HTTPS proxy URL or REPO_CODE_CHATGPT_PROXY=direct.')
    return proxy


def _http():
    try:
        import httpx
    except ImportError:
        raise RepoError('SDK_MISSING', 'Install repo-code[chatgpt]') from None
    proxy = chatgpt_proxy()
    return httpx.Client(timeout=30, follow_redirects=False, **({'proxy': proxy} if proxy else {}))


def _json_request(client, method, url, **kwargs):
    """Never echo OAuth bodies, redirect URLs, tokens or exception strings."""
    try:
        response = client.request(method, url, **kwargs)
        if not 200 <= response.status_code < 300:
            code = 'CHATGPT_AUTH_HTTP_ERROR'
            if response.status_code == 400:
                try:
                    if response.json().get('error') == 'invalid_grant':
                        code = 'CHATGPT_LOGIN_REQUIRED'
                except (ValueError, AttributeError):
                    pass
            stage = {TOKEN_ENDPOINT: 'token_exchange',
                     ISSUER+'/.well-known/openid-configuration': 'openid_discovery',
                     ISSUER+'/.well-known/jwks.json': 'jwks'}.get(url, 'authentication')
            raise RepoError(code, f'Authentication HTTP {response.status_code} at {stage}; credentials were not logged.')
        return response.json()
    except RepoError:
        raise
    except Exception:
        raise RepoError('CHATGPT_AUTH_TRANSPORT', 'Authentication request failed; credentials were not logged.') from None


def _dpapi(data, decrypt=False):
    """Windows credentials are encrypted for the current Windows user."""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    func = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    func.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    func.restype = wintypes.BOOL
    if not func(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RepoError('CHATGPT_CREDENTIAL_PROTECTION_FAILED')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class ChatGPTAuth:
    """One account registration per auth-root; use separate roots to select accounts.

    This root is deliberately separate from Run state, memory and delivery exports.
    All refresh/login/logout writers share the same OS lock. Busy callers fail
    explicitly rather than racing a rotating refresh token.
    """
    def __init__(self, root):
        self.root = Path(root).absolute()

    def _read(self):
        path = self.root / 'session.json'
        if not path.exists():
            return {}
        if path.is_symlink():
            raise RepoError('LINKED_PRIVATE_PATH')
        try:
            if os.name != 'nt' and (path.stat().st_mode & 0o077):
                raise RepoError('CHATGPT_INSECURE_CREDENTIAL_FILE')
            data = path.read_bytes()
            if os.name == 'nt':
                data = _dpapi(data, decrypt=True)
            record = json.loads(data)
            if not isinstance(record, dict):
                raise ValueError('credential record must be an object')
            return record
        except RepoError:
            raise
        except Exception:
            raise RepoError('CHATGPT_INVALID_CREDENTIAL_FILE') from None

    def _write(self, record):
        private_dir(self.root)
        data = json.dumps(record, ensure_ascii=False).encode('utf-8')
        atomic_write(self.root / 'session.json', _dpapi(data) if os.name == 'nt' else data)

    def _lock(self):
        private_dir(self.root)
        return execution_lock(self.root / 'session.lock')

    def status(self):
        # Read-only: status must not create directories or refresh credentials.
        r = self._read()
        proof = r.get('last_verified_inference') if r.get('access_token') and PLAN_SCOPE in r.get('scopes', []) else None
        return {'logged_in': bool(r.get('access_token')), 'email': r.get('email'),
                'plan_usage_enabled': PLAN_SCOPE in r.get('scopes', []),
                'expires_at': r.get('expires_at'), 'auth_root': str(self.root),
                'billing': 'chatgpt_plan', 'live_inference_verified': bool(proof),
                'last_verified_inference': proof}

    def note_inference_success(self, model, response_id):
        """Host diagnostic receipt, never a promise of future account availability."""
        with self._lock():
            record = self._read()
            if not record.get('access_token') or PLAN_SCOPE not in record.get('scopes', []):
                return  # A concurrent sign-out must remain signed out.
            record['last_verified_inference'] = {'model': model, 'response_id': response_id,
                                                 'verified_at': time.time()}
            self._write(record)

    @staticmethod
    def _validate_id_token(client, token, client_id, nonce=None):
        try:
            import jwt
        except ImportError:
            raise RepoError('SDK_MISSING', 'Install repo-code[chatgpt]') from None
        discovery = _json_request(client, 'GET', ISSUER + '/.well-known/openid-configuration')
        if discovery.get('issuer') != ISSUER:
            raise RepoError('CHATGPT_INVALID_ISSUER')
        jwks_url = discovery.get('jwks_uri', '')
        parsed = urlsplit(jwks_url)
        if parsed.scheme != 'https' or parsed.netloc != 'auth.openai.com' or parsed.username:
            raise RepoError('CHATGPT_INVALID_JWKS_ENDPOINT')
        keys = _json_request(client, 'GET', jwks_url)
        try:
            header = jwt.get_unverified_header(token)
            # Algorithm is pinned independently of the untrusted JWT header.
            key = next(k for k in keys['keys'] if k.get('kid') == header.get('kid') and k.get('kty') == 'RSA')
            claims = jwt.decode(token, jwt.PyJWK.from_dict(key).key, algorithms=['RS256'],
                                audience=client_id, issuer=ISSUER,
                                options={'require': ['sub', 'exp', 'iat', 'iss', 'aud']})
            if not claims['sub'] or (nonce is not None and not hmac.compare_digest(str(claims.get('nonce', '')), nonce)):
                raise ValueError('identity mismatch')
            return claims
        except Exception:
            raise RepoError('CHATGPT_INVALID_ID_TOKEN', 'ID token signature or identity validation failed.') from None

    def _token_record(self, old, token, claims=None):
        if not isinstance(token.get('access_token'), str) or not token['access_token']:
            raise RepoError('CHATGPT_INVALID_TOKEN_RESPONSE')
        if str(token.get('token_type', '')).lower() != 'bearer':
            raise RepoError('CHATGPT_INVALID_TOKEN_RESPONSE')
        ttl = token.get('expires_in')
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 0 < ttl <= 86400:
            raise RepoError('CHATGPT_INVALID_TOKEN_RESPONSE')
        scopes = token.get('scope')
        if scopes is not None and not isinstance(scopes, str):
            raise RepoError('CHATGPT_INVALID_TOKEN_RESPONSE')
        granted = scopes.split() if scopes is not None else old.get('scopes', [])
        if PLAN_SCOPE in granted and not token.get('refresh_token'):
            raise RepoError('CHATGPT_INVALID_TOKEN_RESPONSE', 'Renewable session was not granted; sign in again.')
        record = {**old, 'access_token': token['access_token'], 'refresh_token': token.get('refresh_token'),
                  'expires_at': time.time() + ttl,
                  'scopes': granted}
        if token.get('id_token'):
            record['id_token'] = token['id_token']
        if claims:
            record.update(subject=claims['sub'], email=claims.get('email'))
        return record

    def login(self, *, timeout=180, port=0, open_browser=True, notify=print, client=None):
        if timeout <= 0 or timeout > 600:
            raise RepoError('INVALID_LOGIN_TIMEOUT')
        with self._lock():
            old = self._read()
            if not old.get('ext_agent_host_id'):
                old['ext_agent_host_id'] = 'urn:uuid:' + str(uuid.uuid4())
                self._write(old)  # Persist the host identity BEFORE the first consent.
            state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            received = {}

            class Callback(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass  # Standard HTTP logs would disclose the authorization code.

                def do_GET(self):
                    parsed = urlsplit(self.path)
                    values = parse_qs(parsed.query, keep_blank_values=True)
                    valid = (parsed.path == '/auth/callback' and
                             all(len(v) == 1 for v in values.values()) and
                             hmac.compare_digest(values.get('state', [''])[0].encode(), state.encode()))
                    if valid:
                        received.update({k: v[0] for k, v in values.items()})
                    self.send_response(200 if valid else 400)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(b'Authorization received. Return to repo-code.' if valid else b'Invalid authorization callback.')

            class LoopbackServer(HTTPServer):
                def get_request(self):
                    sock, address = super().get_request()
                    sock.settimeout(min(5, timeout))
                    return sock, address

            with LoopbackServer(('127.0.0.1', port), Callback) as server:
                server.timeout = 0.5
                redirect_uri = f'http://127.0.0.1:{server.server_port}/auth/callback'
                params = {'client_id': old.get('client_id', 'dynamic_agent_client'),
                          'ext_agent_host_id': old['ext_agent_host_id'], 'response_type': 'code',
                          'redirect_uri': redirect_uri, 'scope': SCOPES, 'resource': RESOURCE,
                          'state': state, 'nonce': nonce, 'code_challenge_method': 'S256',
                          'code_challenge': challenge}
                if not old.get('client_id'):
                    params['agent_name_hint'] = 'repo-code'
                # Do not put retained ID tokens in printed authorization URLs.
                url = AUTHORIZE_ENDPOINT + '?' + urlencode(params)
                notify('Continue with ChatGPT: authorize repo-code to use your ChatGPT plan. Usage is subject to your plan/app limits.')
                if open_browser:
                    if not webbrowser.open(url):
                        raise RepoError('CHATGPT_BROWSER_FAILED', 'Retry with auth login --no-browser.')
                else:
                    notify(url)  # Contains only fresh PKCE/state metadata, never tokens.
                deadline = time.monotonic() + timeout
                while not received and time.monotonic() < deadline:
                    server.handle_request()
            if not received:
                raise RepoError('CHATGPT_LOGIN_TIMEOUT')
            if received.get('error'):
                raise RepoError('CHATGPT_CONSENT_DENIED', 'ChatGPT authorization was declined; existing credentials were preserved.')
            issued = received.get('client_id', old.get('client_id'))
            if (not issued or issued == 'dynamic_agent_client' or
                    (old.get('client_id') and issued != old['client_id']) or not received.get('code')):
                raise RepoError('CHATGPT_INVALID_CALLBACK')
            owned = client is None
            client = client or _http()
            try:
                token = _json_request(client, 'POST', TOKEN_ENDPOINT, data={
                    'grant_type': 'authorization_code', 'client_id': issued, 'code': received['code'],
                    'code_verifier': verifier, 'redirect_uri': redirect_uri, 'resource': RESOURCE})
                if not token.get('id_token'):
                    raise RepoError('CHATGPT_INVALID_ID_TOKEN')
                claims = self._validate_id_token(client, token['id_token'], issued, nonce)
                if old.get('subject') and claims['sub'] != old['subject']:
                    raise RepoError('CHATGPT_ACCOUNT_MISMATCH', 'Use a different --auth-root for a different ChatGPT account.')
                # A valid identity without plan permission is saved as signed-in,
                # disabled. It must never be used for inference.
                record = self._token_record({**old, 'client_id': issued}, token, claims)
                self._write(record)
            finally:
                if owned:
                    client.close()
        return self.status()

    def access_token(self, *, client=None):
        with self._lock():
            record = self._read()
            if not record.get('access_token'):
                raise RepoError('CHATGPT_LOGIN_REQUIRED', 'Run repo-code --auth-root PATH auth login.')
            if PLAN_SCOPE not in record.get('scopes', []):
                raise RepoError('CHATGPT_PLAN_PERMISSION_REQUIRED', 'Run auth login and enable ChatGPT plan usage.')
            if record.get('expires_at', 0) <= time.time() + 60:
                owned = client is None
                client = client or _http()
                try:
                    token = _json_request(client, 'POST', TOKEN_ENDPOINT, data={
                        'grant_type': 'refresh_token', 'client_id': record['client_id'],
                        'refresh_token': record['refresh_token'], 'resource': RESOURCE})
                    claims = None
                    if token.get('id_token'):
                        claims = self._validate_id_token(client, token['id_token'], record['client_id'])
                        if claims['sub'] != record['subject']:
                            raise RepoError('CHATGPT_ACCOUNT_MISMATCH')
                    record = self._token_record(record, token, claims)
                    self._write(record)  # Replace rotating tokens together, atomically.
                finally:
                    if owned:
                        client.close()
                if PLAN_SCOPE not in record['scopes']:
                    raise RepoError('CHATGPT_PLAN_PERMISSION_REQUIRED')
            return record['access_token']

    def logout(self, *, client=None):
        confirmed = False
        with self._lock():
            record = self._read()
            if record.get('refresh_token'):
                owned = client is None
                client = client or _http()
                try:
                    discovery = _json_request(client, 'GET', ISSUER + '/.well-known/openid-configuration')
                    endpoint = discovery.get('revocation_endpoint', '')
                    parsed = urlsplit(endpoint)
                    if parsed.scheme == 'https' and parsed.netloc == 'auth.openai.com':
                        for attempt in range(3):
                            try:
                                response = client.request('POST', endpoint, data={
                                    'token': record['refresh_token'], 'token_type_hint': 'refresh_token',
                                    'client_id': record['client_id']})
                                confirmed = response.status_code == 200
                                if confirmed or response.status_code < 500:
                                    break
                            except Exception:
                                pass
                            if attempt < 2:
                                time.sleep(0.2 * (2 ** attempt))
                except RepoError:
                    pass
                finally:
                    if owned:
                        client.close()
            self._write({k: record[k] for k in ('ext_agent_host_id', 'client_id', 'subject', 'email') if k in record})
        return {'logged_in': False, 'remote_revocation_confirmed': confirmed,
                'message': 'Signed out locally.' if confirmed else
                'Signed out locally; remote revocation was not confirmed. Disconnect repo-code in ChatGPT Settings if needed.'}
