import yfinance as yf
import requests
import pandas as pd
from datetime import datetime
import pytz
import os

# ====================== CONFIGURATION ======================
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

if not WEBHOOK_URL:
    raise ValueError("La variable d'environnement DISCORD_WEBHOOK_URL est manquante")

paris = pytz.timezone("Europe/Paris")

tickers = {
    "VIX": "^VIX",
    "SKEW": "^SKEW",
    "VVIX": "^VVIX",
    "VIX9D": "^VIX9D",
    "VIX3M": "^VIX3M",
    "MOVE": "^MOVE"
}

# ====================== FONCTIONS ======================
def get_data():
    data = {}
    for name, ticker in tickers.items():
        try:
            df = yf.Ticker(ticker).history(period="5d")
            last = df["Close"].dropna().iloc[-1]
            data[name] = round(float(last), 2)
        except Exception as e:
            print(f"Erreur {name}: {e}")
            data[name] = None
    return data

def interpret(data):
    comments = []
    vix = data.get("VIX")
    skew = data.get("SKEW")
    move = data.get("MOVE")

    if vix is not None:
        if vix < 15:
            comments.append("🟢 **VIX bas** → Marché très calme (complacency)")
        elif vix < 20:
            comments.append("🟡 **VIX normal**")
        elif vix < 25:
            comments.append("🟠 **VIX en hausse** → Stress modéré")
        elif vix < 30:
            comments.append("🔴 **VIX élevé** → Stress important")
        else:
            comments.append("🚨 **VIX très élevé** → Panique")

    if skew is not None and skew >= 145:
        comments.append("⚠️ **SKEW élevé** → Forte demande de protection anti-krach")

    if move is not None and move >= 100:
        comments.append("📉 **MOVE élevé** → Stress sur les obligations")

    return "\n".join(comments) if comments else "Aucune alerte particulière pour le moment."

def send_to_discord(data):
    now = datetime.now(paris).strftime("%d/%m/%Y à %H:%M")

    embed = {
        "title": f"📊 Brief Volatilité — {now}",
        "color": 3447003,  # bleu
        "fields": [
            {"name": "VIX", "value": f"`{data['VIX']}`" if data['VIX'] is not None else "`N/A`", "inline": True},
            {"name": "SKEW", "value": f"`{data['SKEW']}`" if data['SKEW'] is not None else "`N/A`", "inline": True},
            {"name": "VVIX", "value": f"`{data['VVIX']}`" if data['VVIX'] is not None else "`N/A`", "inline": True},
            {"name": "VIX9D", "value": f"`{data['VIX9D']}`" if data['VIX9D'] is not None else "`N/A`", "inline": True},
            {"name": "VIX3M", "value": f"`{data['VIX3M']}`" if data['VIX3M'] is not None else "`N/A`", "inline": True},
            {"name": "MOVE", "value": f"`{data['MOVE']}`" if data['MOVE'] is not None else "`N/A`", "inline": True},
        ],
        "description": interpret(data),
        "footer": {
            "text": "Données Yahoo Finance • Quasi temps réel"
        }
    }

    payload = {
        "username": "Volatilité Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2920/2920277.png",
        "embeds": [embed]
    }

    response = requests.post(WEBHOOK_URL, json=payload)
    
    if response.status_code in [200, 204]:
        print("Message envoyé avec succès sur Discord")
    else:
        print(f"Erreur Discord : {response.status_code}")
        print(response.text)
        raise Exception("Échec de l'envoi Discord")

# ====================== MAIN ======================
if __name__ == "__main__":
    print("Récupération des données...")
    data = get_data()
    print("Données :", data)
    send_to_discord(data)
