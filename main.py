import yfinance as yf
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
import os

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not WEBHOOK_URL:
    raise ValueError("DISCORD_WEBHOOK_URL manquant")

paris = pytz.timezone("Europe/Paris")

tickers = {
    "VIX9D": "^VIX9D",
    "VIX": "^VIX",
    "VIX3M": "^VIX3M",
    "VVIX": "^VVIX",
    "SKEW": "^SKEW",
    "MOVE": "^MOVE"
}

# ====================== INTERPRÉTATIONS ======================
def zscore_interpret(z):
    if z is None:
        return "N/A"
    if z <= -2.0:
        return "🚨 **Extrêmement bas**"
    elif z <= -1.0:
        return "🔵 **Bas**"
    elif z <= 0.5:
        return "🟢 **Normal / Bas**"
    elif z <= 1.5:
        return "🟡 **Au-dessus de la moyenne**"
    elif z <= 2.5:
        return "🟠 **Élevé**"
    else:
        return "🔴 **Extrêmement élevé**"

def get_data():
    data = {}
    for name, ticker in tickers.items():
        try:
            df = yf.Ticker(ticker).history(period="30d")
            df = df.dropna(subset=["Close"])
            if len(df) < 5:
                continue

            close = df["Close"]
            current = float(close.iloc[-1])
            prev = float(close.iloc[-2])

            change = round((current - prev) / prev * 100, 2)

            # Z-score sur 20 jours
            window = close.iloc[-20:]
            zscore = round((current - window.mean()) / window.std(), 2) if window.std() != 0 else 0.0

            data[name] = {
                "value": round(current, 2),
                "change": change,
                "zscore": zscore,
                "z_interpret": zscore_interpret(zscore)
            }
        except Exception as e:
            print(f"Erreur {name}: {e}")
            data[name] = None
    return data


def analyze_vix_term_structure(data):
    v9d = data.get("VIX9D", {})
    vix = data.get("VIX", {})
    v3m = data.get("VIX3M", {})

    if not all([v9d, vix, v3m]):
        return "Données insuffisantes pour l'analyse de courbe."

    v9 = v9d["value"]
    v = vix["value"]
    v3 = v3m["value"]

    short_spread = v - v9
    long_spread = v3 - v
    curvature = short_spread - long_spread   # Approximation de convexité

    analysis = []

    # Direction de la courbe
    if v3 > v > v9:
        analysis.append("📈 **Courbe ascendante normale** (contango)")
    elif v9 > v > v3:
        analysis.append("📉 **Courbe inversée** (backwardation) → Anticipation de forte volatilité court terme")
    else:
        analysis.append("➡️ Courbe plate ou irrégulière")

    # Convexité
    if curvature > 1.5:
        analysis.append("🔄 **Forte convexité** → Risque de krach important (queue de distribution grasse)")
    elif curvature > 0.5:
        analysis.append("🔄 Convexité modérée")
    elif curvature < -1.0:
        analysis.append("📉 **Concavité** → Marché relativement calme ou retour à la normale anticipé")

    # Interprétation risque
    if v > 25 and v3 > v:
        analysis.append("⚠️ **Régime de stress moyen-terme**")
    elif v > 30:
        analysis.append("🚨 **Régime de panique** (forte prime de risque)")

    return "\n".join(analysis)


def send_to_discord(data):
    now = datetime.now(paris).strftime("%d/%m/%Y à %H:%M")

    fields = []
    order = ["VIX9D", "VIX", "VIX3M", "VVIX", "SKEW", "MOVE"]

    for name in order:
        d = data.get(name)
        if not d:
            continue
        value_line = f"`{d['value']}` | {d['change']:+.2f}% | Z `{d['zscore']}`"
        fields.append({
            "name": f"{name} {d['z_interpret']}",
            "value": value_line,
            "inline": True
        })

    term_analysis = analyze_vix_term_structure(data)

    embed = {
        "title": f"📊 Volatility Intelligence Report — {now}",
        "color": 3447003,
        "fields": fields,
        "description": "**Analyse Term Structure VIX :**\n" + term_analysis,
        "footer": {"text": "Données Yahoo Finance • Z-score 20 jours • Analyse convexité"}
    }

    payload = {
        "username": "Volatilité Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2920/2920277.png",
        "embeds": [embed]
    }

    response = requests.post(WEBHOOK_URL, json=payload)
    print("Message envoyé" if response.status_code in [200, 204] else f"Erreur {response.status_code}")


if __name__ == "__main__":
    print("Récupération + analyse...")
    data = get_data()
    send_to_discord(data)
