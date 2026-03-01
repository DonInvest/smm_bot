import os
import webbrowser

import requests
from dotenv import load_dotenv


load_dotenv()

NEYNAR_API_KEY = os.getenv("NEYNAR_API_KEY", "").strip()


def main():
    if not NEYNAR_API_KEY:
        print("❌ В .env не задан NEYNAR_API_KEY")
        return

    url = "https://api.neynar.com/v2/farcaster/signer/"
    headers = {"x-api-key": NEYNAR_API_KEY, "Content-Type": "application/json"}

    resp = requests.post(url, headers=headers, timeout=20)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    if resp.status_code != 200:
        print("❌ Ошибка создания signer:")
        print(data)
        return

    signer_uuid = data.get("signer_uuid")
    approval_url = data.get("signer_approval_url")
    status = data.get("status")

    print("✅ Signer создан.")
    print("status:", status)
    print("\nДобавь это в .env:")
    print(f"NEYNAR_SIGNER_UUID={signer_uuid}")

    print("\nПолный ответ Neynar (для отладки):")
    print(data)

    if approval_url:
        print("\nОткрой ссылку и одобри signer в Warpcast:")
        print(approval_url)
        try:
            webbrowser.open(approval_url)
        except Exception:
            pass
    else:
        print(
            "\n⚠️ Поле signer_approval_url не пришло в ответе.\n"
            "Зайди в Neynar → Apps → твой app → Signers: там должна быть кнопка/ссылка Approve для указанного signer_uuid."
        )

    print("\nПосле одобрения перезапусти бота и попробуй постинг в Farcaster.")


if __name__ == "__main__":
    main()

