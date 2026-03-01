import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

print("--- Список доступных моделей для твоего ключа ---")
try:
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"Доступна: {m.name}")
except Exception as e:
    print(f"Ошибка при получении списка: {e}")