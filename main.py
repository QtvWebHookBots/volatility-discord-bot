import yfinance as yf
import requests
import pandas as pd
import numpy as np
from datetime import datetime
import pytz
import os
import json

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not WEBHOOK_URL:
    raise ValueError("DISCORD_WEBHOOK_URL manquant")

paris = pytz.timezone("Europe/Paris")

# Fichier pour stocker l'état précédent (permet les alertes "changement de régime")
STATE_FILE = "vol_bot_state.json"

tickers = {
    "VIX9D": "^VIX9D",
    "VIX": "^VIX",
    "VIX3M": "^VIX3M",
    "VVIX": "^VVIX",
    "SKEW": "^SKEW",
    "MOVE": "^MOVE"
}

# Historique plus long pour avoir des percentiles significatifs (1 an)
HISTORY_PERIOD = "1y"
ZSCORE_WINDOW = 20


# ====================== INTERPRÉTATIONS ======================

def zscore_interpret(z):
    if z is None:
        return "N/A"
    if z <= -2.0:
        return "🚨 Extrêmement bas (récent)"
    elif z <= -1.0:
        return "🔵 Bas (récent)"
    elif z <= 0.5:
        return "🟢 Normal (récent)"
    elif z <= 1.5:
        return "🟡 Au-dessus de la moyenne (récent)"
    elif z <= 2.5:
        return "🟠 Élevé (récent)"
    else:
        return "🔴 Extrêmement élevé (récent)"


def percentile_interpret(pct):
    """Percentile sur 1 an = bien plus parlant qu'un z-score sur 20j."""
    if pct is None:
        return "N/A"
    if pct <= 5:
        return f"plus bas que {100 - pct:.0f}% de l'année écoulée (niveau rare)"
    elif pct <= 20:
        return f"dans le bas de sa fourchette annuelle ({pct:.0f}e percentile)"
    elif pct <= 80:
        return f"dans sa fourchette normale ({pct:.0f}e percentile)"
    elif pct <= 95:
        return f"dans le haut de sa fourchette annuelle ({pct:.0f}e percentile)"
    else:
        return f"plus haut que {pct:.0f}% de l'année écoulée (niveau rare)"


# ====================== RÉCUPÉRATION DES DONNÉES ======================

def get_data():
    data = {}
    for name, ticker in tickers.items():
        try:
            df = yf.Ticker(ticker).history(period=HISTORY_PERIOD)
            df = df.dropna(subset=["Close"])
            if len(df) < ZSCORE_WINDOW:
                continue

            close = df["Close"]
            current = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            change_1d = round((current - prev) / prev * 100, 2)

            # Variation sur 5 jours (momentum court terme)
            if len(close) >= 6:
                prev5 = float(close.iloc[-6])
                change_5d = round((current - prev5) / prev5 * 100, 2)
            else:
                change_5d = None

            # Z-score court terme (20j) -> réactivité
            window20 = close.iloc[-ZSCORE_WINDOW:]
            zscore = round((current - window20.mean()) / window20.std(), 2) if window20.std() != 0 else 0.0

            # Percentile sur la période longue -> contexte annuel
            percentile = round((close < current).sum() / len(close) * 100, 1)

            # Plus haut / plus bas sur la période
            year_high = float(close.max())
            year_low = float(close.min())

            data[name] = {
                "value": round(current, 2),
                "change_1d": change_1d,
                "change_5d": change_5d,
                "zscore": zscore,
                "z_interpret": zscore_interpret(zscore),
                "percentile": percentile,
                "pct_interpret": percentile_interpret(percentile),
                "year_high": round(year_high, 2),
                "year_low": round(year_low, 2),
            }
        except Exception as e:
            print(f"Erreur {name}: {e}")
            data[name] = None
    return data


# ====================== ANALYSE TERM STRUCTURE ======================

def analyze_vix_term_structure(data):
    v9d = data.get("VIX9D")
    vix = data.get("VIX")
    v3m = data.get("VIX3M")

    if not all([v9d, vix, v3m]):
        return "Données insuffisantes pour l'analyse de courbe.", None

    v9 = v9d["value"]
    v = vix["value"]
    v3 = v3m["value"]

    # Ratio de contango standard (plus interprétable qu'un spread brut)
    contango_ratio = round((v3 / v - 1) * 100, 1)

    lines = []

    if v3 > v > v9:
        lines.append(f"📈 Courbe ascendante normale (contango de {contango_ratio:+.1f}%) → marché serein sur le terme")
    elif v9 > v > v3:
        lines.append(f"📉 Courbe inversée (backwardation, {contango_ratio:+.1f}%) → stress court terme, le marché paie plus cher pour se couvrir maintenant que dans 3 mois")
    else:
        lines.append(f"➡️ Courbe plate/irrégulière (écart 3M vs spot: {contango_ratio:+.1f}%)")

    if v > 30:
        lines.append("🚨 Régime de panique (VIX > 30)")
    elif v > 25 and v3 > v:
        lines.append("⚠️ Régime de stress moyen terme")
    elif v < 15:
        lines.append("😴 Régime de complaisance (VIX < 15)")

    return "\n".join(lines), contango_ratio


# ====================== ANALYSE CROISÉE ACTIONS / TAUX ======================

def analyze_cross_asset(data):
    """Compare le stress sur actions (VIX) vs le stress sur taux (MOVE).
    Un stress qui n'apparaît que sur un seul actif est moins inquiétant
    qu'un stress systémique visible sur les deux."""
    vix = data.get("VIX")
    move = data.get("MOVE")
    if not vix or not move:
        return None

    vix_high = vix["percentile"] >= 70
    move_high = move["percentile"] >= 70
    vix_low = vix["percentile"] <= 30
    move_low = move["percentile"] <= 30

    if vix_high and move_high:
        return "🔴 Stress systémique : actions ET obligations sous tension simultanément — signal à prendre au sérieux"
    elif vix_high and not move_high:
        return "🟠 Stress cantonné aux actions (le marché obligataire reste calme) — souvent un signal plus spécifique/sectoriel"
    elif move_high and not vix_high:
        return "🟠 Stress cantonné aux taux (les actions ne réagissent pas encore) — à surveiller, historiquement souvent précurseur"
    elif vix_low and move_low:
        return "🟢 Calme généralisé sur actions et taux"
    else:
        return "➡️ Pas de signal croisé notable actions/taux"


# ====================== SCORE DE RISQUE COMPOSITE ======================

def compute_risk_score(data):
    """Combine VIX, VVIX, SKEW, MOVE en un seul score 0-100 pondéré
    par leur percentile annuel, pour une lecture en un coup d'œil."""
    weights = {"VIX": 0.40, "VVIX": 0.20, "SKEW": 0.15, "MOVE": 0.25}
    total_weight = 0
    score = 0

    for name, w in weights.items():
        d = data.get(name)
        if d and d.get("percentile") is not None:
            score += d["percentile"] * w
            total_weight += w

    if total_weight == 0:
        return None, "N/A"

    score = round(score / total_weight, 1)

    if score < 20:
        label = "😴 Complaisance"
    elif score < 40:
        label = "🟢 Calme"
    elif score < 60:
        label = "🟡 Neutre"
    elif score < 80:
        label = "🟠 Tendu"
    else:
        label = "🔴 Stress élevé"

    return score, label


# ====================== SYNTHÈSE EN LANGAGE NATUREL ======================

def generate_natural_summary(data, contango_ratio, cross_asset, risk_score, risk_label):
    vix = data.get("VIX")
    vvix = data.get("VVIX")
    skew = data.get("SKEW")

    parts = []

    if risk_score is not None:
        parts.append(f"Le score de risque composite est à **{risk_score}/100** ({risk_label}).")

    if vix:
        parts.append(f"Le VIX est {vix['pct_interpret']}, avec une variation de {vix['change_1d']:+.2f}% sur la séance et {vix['change_5d']:+.2f}% sur 5 jours.")

    if vvix and skew:
        # Cas particulier intéressant : VVIX bas + SKEW haut = marché calme en apparence mais risque de queue sous-jacent
        if vvix["percentile"] <= 30 and skew["percentile"] >= 70:
            parts.append("⚠️ Signal notable : la volatilité de la volatilité (VVIX) est basse alors que le SKEW (risque de queue) est élevé — le marché semble calme en surface mais price un risque de choc soudain.")
        elif vvix["percentile"] >= 70 and skew["percentile"] >= 70:
            parts.append("Le VVIX et le SKEW sont tous les deux élevés — le marché anticipe à la fois de l'instabilité générale et un risque de mouvement extrême.")

    if cross_asset:
        parts.append(cross_asset)

    if contango_ratio is not None:
        if contango_ratio < -3:
            parts.append("La forte backwardation suggère une couverture court terme coûteuse — signe que les investisseurs paient cher pour se protéger dans l'immédiat.")

    return " ".join(parts)


# ====================== PERSISTANCE D'ÉTAT (pour alertes) ======================

def load_previous_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(risk_score, data):
    state = {
        "risk_score": risk_score,
        "timestamp": datetime.now(paris).isoformat(),
        "values": {k: v["value"] for k, v in data.items() if v},
    }
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Impossible de sauvegarder l'état: {e}")


def check_alert(risk_score, previous_state):
    """Ne déclenche un @here que sur un vrai changement de régime,
    pas à chaque envoi, pour éviter le bruit."""
    if risk_score is None:
        return None
    prev_score = previous_state.get("risk_score")
    if prev_score is None:
        return None

    delta = risk_score - prev_score
    if delta >= 20:
        return f"🚨 **ALERTE** : le score de risque a bondi de {prev_score} à {risk_score} depuis le dernier envoi."
    elif delta <= -20:
        return f"✅ **Détente notable** : le score de risque est passé de {prev_score} à {risk_score} depuis le dernier envoi."
    return None


# ====================== ENVOI DISCORD ======================

def send_to_discord(data):
    now = datetime.now(paris).strftime("%d/%m/%Y à %H:%M")

    fields = []
    order = ["VIX9D", "VIX", "VIX3M", "VVIX", "SKEW", "MOVE"]

    for name in order:
        d = data.get(name)
        if not d:
            continue
        value_line = (
            f"`{d['value']}` | j: {d['change_1d']:+.2f}% | 5j: {d['change_5d']:+.2f}%\n"
            f"Z(20j) `{d['zscore']}` · {d['pct_interpret']}"
        )
        fields.append({
            "name": f"{name}",
            "value": value_line,
            "inline": True
        })

    term_analysis, contango_ratio = analyze_vix_term_structure(data)
    cross_asset = analyze_cross_asset(data)
    risk_score, risk_label = compute_risk_score(data)
    summary = generate_natural_summary(data, contango_ratio, cross_asset, risk_score, risk_label)

    previous_state = load_previous_state()
    alert = check_alert(risk_score, previous_state)

    # Couleur de l'embed dynamique selon le score de risque
    if risk_score is None:
        color = 3447003
    elif risk_score < 40:
        color = 3066993   # vert
    elif risk_score < 70:
        color = 15844367  # orange/jaune
    else:
        color = 15158332  # rouge

    description = f"**{summary}**\n\n**Term Structure VIX :**\n{term_analysis}"

    embed = {
        "title": f"📊 Volatility Intelligence Report — {now}",
        "color": color,
        "fields": fields,
        "description": description,
        "footer": {"text": "Yahoo Finance • Z-score 20j = réactivité court terme • Percentile 1an = contexte annuel"}
    }

    payload = {
        "username": "Volatilité Bot",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2920/2920277.png",
        "content": alert if alert else None,
        "embeds": [embed]
    }

    response = requests.post(WEBHOOK_URL, json=payload)
    print("Message envoyé" if response.status_code in [200, 204] else f"Erreur {response.status_code}")

    save_state(risk_score, data)


if __name__ == "__main__":
    print("Récupération + analyse...")
    data = get_data()
    send_to_discord(data)
