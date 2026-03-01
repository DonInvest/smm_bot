import base64
import hashlib
import os
import secrets
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

import requests
from dotenv import load_dotenv


load_dotenv()

CLIENT_ID = os.getenv("X_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("X_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.getenv("X_REDIRECT_URI", "http://localhost:8000/callback").strip()
SCOPES = "tweet.read tweet.write users.read offline.access"

AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def get_code_verifier_and_challenge():
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


class CallbackHandler(BaseHTTPRequestHandler):
    authorization_code = None
    state_expected = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if not code or state != self.state_expected:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid authorization response.")
            return

        CallbackHandler.authorization_code = code

        self.send_response(200)
        self.end_headers()
        self.wfile.write(
            b"Auth complete. You can close this tab and return to the terminal."
        )

        # Останавливаем сервер после получения кода
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main():
    if not CLIENT_ID:
        print("❌ В .env не задан X_CLIENT_ID (из раздела OAuth 2.0 Keys).")
        return
    if not CLIENT_SECRET:
        print("❌ В .env не задан X_CLIENT_SECRET (из раздела OAuth 2.0 Keys).")
        return

    verifier, challenge = get_code_verifier_and_challenge()
    state = _b64url(secrets.token_bytes(16))

    # Запускаем локальный HTTP‑сервер для приёма кода
    CallbackHandler.state_expected = state
    server = HTTPServer(("localhost", 8000), CallbackHandler)

    auth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urlencode(auth_params)}"

    print("Открою окно браузера для авторизации в X...")
    webbrowser.open(url)
    print("Если окно не открылось, скопируй и вставь этот URL в браузер:")
    print(url)

    print("\nОжидаю редирект на", REDIRECT_URI)
    server.handle_request()

    code = CallbackHandler.authorization_code
    if not code:
        print("❌ Не удалось получить authorization code.")
        return

    print("✅ Authorization code получен, запрашиваю access_token...")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode("utf-8")).decode(
        "utf-8"
    )
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic}",
    }

    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=20)
    try:
        payload = resp.json()
    except Exception:
        payload = {"raw": resp.text}

    if resp.status_code != 200:
        print("❌ Ошибка при получении токена:")
        print(payload)
        return

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")

    print("\n✅ Успех!")
    if access_token:
        print("\nДобавь это в свой .env как X_USER_ACCESS_TOKEN:")
        print(f"X_USER_ACCESS_TOKEN={access_token}")
    if refresh_token:
        print("\nА это можешь сохранить отдельно как refresh-токен (опционально):")
        print(f"X_REFRESH_TOKEN={refresh_token}")


if __name__ == "__main__":
    main()

