import yfinance as yf
import requests
import pandas as pd
import numpy as np
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
            # On prend 25 jours pour avoir assez de données pour le z-score + variation
            df = yf.Ticker(ticker).history(period="25d")
            df = df.dropna(subset=["Close"])
            
            if len(df) < 2:
                data[name] = {"value": None, "change": None, "zscore": None}
                continue

            close = df["Close"]
            current = float(close.iloc[-1])
            previous = float(close.iloc[-2])

            # Variation journalière en %
            daily_change = round((current - previous) / previous * 100, 2)

            # Z-score sur les 20 dernières clôtures
            window = close.iloc[-20:]
            mean = window.mean()
            std = window.std()
            zscore = round((current - mean) / std, 2) if std != 0 else 0.0

            data[name] = {
                "value": round(current, 2),
                "change": daily_change,
                "zscore": zscore
            }
        except Exception as e:
            print(f"Erreur {name}: {e}")
            data[name] = {"value": None, "change": None, "zscore": None}
    return data


def interpret(data):
    comments = []
    vix = data.get("VIX", {}).get("value")

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

    skew = data.get("SKEW", {}).get("value")
    if skew is not None and skew >= 145:
        comments.append("⚠️ **SKEW élevé** → Forte demande de protection anti-krach")

    move = data.get("MOVE", {}).get("value")
    if move is not None and move >= 100:
        comments.append("📉 **MOVE élevé** → Stress sur les obligations")

    return "\n".join(comments) if comments else "Aucune alerte particulière pour le moment."


def format_field(value, change, zscore):
    if value is None:
        return "`N/A`"
    
    change_str = f"{change:+.2f}%" if change is not None else "N/A"
    z_str = f"{zscore:+.2f}" if zscore is not None else "N/A"
    
    return f"`{value}` | {change_str} | Z `{z_str}`"


def send_to_discord(data):
    now = datetime.now(paris).strftime("%d/%m/%Y à %H:%M")

    fields = []
    for name in ["VIX", "SKEW", "VVIX", "VIX9D", "VIX3M", "MOVE"]:
        d = data.get(name, {})
        fields.append({
            "name": name,
            "value": format_field(d.get("value"), d.get("change"), d.get("zscore")),
            "inline": True
        })

    embed = {
        "title": f"📊 Brief Volatilité — {now}",
        "color": 3447003,
        "fields": fields,
        "description": interpret(data),
        "footer": {
            "text": "Données Yahoo Finance • Z-score sur 20 jours"
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


# ====================== MAIN ======================
if __name__ == "__main__":
    print("Récupération des données...")
    data = get_data()
    print("Données récupérées :", {k: v["value"] for k, v in data.items()})
    send_to_discord(data)
