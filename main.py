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

# Fichier pour stocker l'état précédent (permet les alertes "changement de régime"
# et le calcul "depuis le dernier envoi")
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

# Pour chaque métrique : ce qu'elle mesure concrètement, et ce que signifie
# une hausse / une baisse en langage clair. Toutes ces métriques sont des
# "jauges de peur" : une hausse = défavorable (plus de stress), une baisse
# = favorable (détente). C'est ce qui permet le code couleur cohérent.
METRIC_INFO = {
    "VIX9D": {
        "label": "Volatilité implicite à 9 jours",
        "meaning_up": "les investisseurs anticipent une agitation imminente, dans les tout prochains jours (souvent lié à un événement daté : earnings, banque centrale, données macro)",
        "meaning_down": "les tensions à très court terme se dissipent, aucun événement imminent ne semble inquiéter le marché",
    },
    "VIX": {
        "label": "Volatilité implicite à 30 jours (référence)",
        "meaning_up": "la prime de risque exigée par les investisseurs sur les actions US augmente sur l'horizon d'un mois",
        "meaning_down": "les investisseurs exigent une prime de risque plus faible, signe de confiance retrouvée",
    },
    "VIX3M": {
        "label": "Volatilité implicite à 3 mois",
        "meaning_up": "l'inquiétude s'installe sur un horizon plus long, pas seulement à court terme",
        "meaning_down": "les anticipations de volatilité à moyen terme se détendent",
    },
    "VVIX": {
        "label": "Volatilité de la volatilité (incertitude sur le VIX lui-même)",
        "meaning_up": "l'incertitude sur la trajectoire future du VIX augmente — les traders d'options anticipent des à-coups plutôt qu'une évolution régulière",
        "meaning_down": "le marché des options anticipe une trajectoire de volatilité plus stable et prévisible",
    },
    "SKEW": {
        "label": "Risque de queue / probabilité d'un krach perçue",
        "meaning_up": "le marché est prêt à payer plus cher pour se couvrir contre un scénario extrême (krach), même si la volatilité \"normale\" ne le reflète pas encore",
        "meaning_down": "la demande de protection contre un scénario extrême diminue",
    },
    "MOVE": {
        "label": "Volatilité implicite obligataire (équivalent VIX pour les taux)",
        "meaning_up": "le marché des taux devient plus nerveux, souvent lié à des doutes sur la politique monétaire ou l'inflation",
        "meaning_down": "le marché obligataire se stabilise, signe de visibilité retrouvée sur les taux",
    },
}


def get_change_badge(pct):
    """Code couleur cohérent pour toutes les jauges de peur :
    hausse = défavorable (rouge), baisse = favorable (vert).
    Discord n'autorise pas la couleur de texte dans un embed,
    donc on simule avec des pastilles + un mot explicite."""
    if pct is None:
        return "", "n/a"
    if pct <= -5:
        return "🟢", "forte détente"
    elif pct <= -1:
        return "🟢", "détente"
    elif pct < 1:
        return "⚪", "stable"
    elif pct < 5:
        return "🟠", "tension"
    else:
        return "🔴", "forte tension"


def get_directional_meaning(name, change):
    """Phrase explicite sur ce que signifie le mouvement de cette métrique précise.
    Générique : fonctionne aussi bien pour une variation 1j que pour une variation
    'depuis le dernier envoi'."""
    info = METRIC_INFO.get(name)
    if not info:
        return ""
    if change is None or abs(change) < 0.5:
        return "Mouvement quasi nul sur la période, pas de signal directionnel."
    elif change > 0:
        return info["meaning_up"].capitalize() + "."
    else:
        return info["meaning_down"].capitalize() + "."


# ====================== INTERPRÉTATIONS ======================

def zscore_interpret(z):
    if z is None:
        return "N/A"
    if z <= -2.0:
        return "🟢 Extrêmement bas (récent)"
    elif z <= -1.0:
        return "🟢 Bas (récent)"
    elif z <= 0.5:
        return "⚪ Normal (récent)"
    elif z <= 1.5:
        return "🟠 Au-dessus de la moyenne (récent)"
    elif z <= 2.5:
        return "🔴 Élevé (récent)"
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
        lines.append(f"➖ Courbe plate/irrégulière (écart 3M vs spot: {contango_ratio:+.1f}%)")

    if v > 30:
        lines.append("🔴 Régime de panique (VIX > 30)")
    elif v > 25 and v3 > v:
        lines.append("🟠 Régime de stress moyen terme")
    elif v < 15:
        lines.append("🟢 Régime de complaisance (VIX < 15)")

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
        return "⚪ Pas de signal croisé notable actions/taux"


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
        label = "🟢 Complaisance"
    elif score < 40:
        label = "🟢 Calme"
    elif score < 60:
        label = "⚪ Neutre"
    elif score < 80:
        label = "🟠 Tendu"
    else:
        label = "🔴 Stress élevé"

    return score, label


# ====================== SYNTHÈSE EN LANGAGE NATUREL (24H) ======================

def generate_natural_summary(data, contango_ratio, cross_asset, risk_score, risk_label):
    vix = data.get("VIX")
    vix9d = data.get("VIX9D")
    vvix = data.get("VVIX")
    skew = data.get("SKEW")
    move = data.get("MOVE")

    parts = []

    # --- Phrase d'ouverture : le verdict, immédiatement ---
    if risk_score is not None:
        parts.append(f"**Score de risque composite : {risk_score}/100 — {risk_label}.**")

    # --- VIX : la métrique de référence, avec sa cause probable ---
    if vix:
        badge, word = get_change_badge(vix["change_1d"])
        direction = "monte" if vix["change_1d"] > 0 else ("baisse" if vix["change_1d"] < 0 else "stagne")
        parts.append(
            f"Le VIX {direction} de {vix['change_1d']:+.2f}% aujourd'hui et se situe {vix['pct_interpret']} "
            f"({badge} {word} par rapport à hier) — sur 5 jours la tendance est de {vix['change_5d']:+.2f}%."
        )

    # --- VIX9D vs VIX : lecture explicite de l'urgence court terme ---
    if vix9d and vix:
        if vix9d["change_1d"] > vix["change_1d"] + 3:
            parts.append(
                f"⚡ Le VIX9D (horizon 9 jours) bouge nettement plus vite que le VIX classique "
                f"({vix9d['change_1d']:+.2f}% contre {vix['change_1d']:+.2f}%), ce qui signale une inquiétude "
                f"concentrée sur un événement précis dans les jours qui viennent plutôt qu'un stress généralisé."
            )

    # --- VVIX / SKEW : le cas de la fausse tranquillité ---
    if vvix and skew:
        if vvix["percentile"] <= 30 and skew["percentile"] >= 70:
            parts.append(
                "⚠️ **Signal de vigilance :** le VVIX (incertitude sur la volatilité elle-même) est bas "
                f"({vvix['percentile']:.0f}e percentile) alors que le SKEW (risque de krach perçu) est élevé "
                f"({skew['percentile']:.0f}e percentile) — traduction concrète : le marché ne s'attend pas à "
                "de l'agitation générale, mais certains investisseurs achètent quand même une assurance contre "
                "un scénario extrême. C'est souvent le signe d'une couverture ciblée plutôt que d'une panique large."
            )
        elif vvix["percentile"] >= 70 and skew["percentile"] >= 70:
            parts.append(
                "🔴 Le VVIX et le SKEW sont élevés simultanément — le marché anticipe à la fois plus d'instabilité "
                "générale ET un risque de mouvement extrême. Combinaison rare, à prendre au sérieux."
            )
        elif vvix["percentile"] <= 30 and skew["percentile"] <= 30:
            parts.append("🟢 VVIX et SKEW sont tous deux bas : ni instabilité anticipée, ni demande de protection contre un choc.")

    # --- Croisement actions / taux ---
    if cross_asset:
        parts.append(cross_asset)

    # --- Term structure ---
    if contango_ratio is not None:
        if contango_ratio < -3:
            parts.append(
                f"📉 Backwardation marquée ({contango_ratio:+.1f}%) : les investisseurs paient plus cher pour se "
                "couvrir immédiatement que pour une couverture à 3 mois — traduction : le danger perçu est jugé "
                "plus grand maintenant que plus tard."
            )
        elif contango_ratio > 15:
            parts.append(
                f"Le contango est très élevé ({contango_ratio:+.1f}%), ce qui est cohérent avec un marché serein "
                "à court terme — mais une remontée brutale de cet écart serait à surveiller."
            )

    return " ".join(parts)


# ====================== PERSISTANCE D'ÉTAT (pour alertes + comparaison inter-envoi) ======================

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


# ====================== COMPARAISON "DEPUIS LE DERNIER ENVOI" ======================

def format_elapsed(hours):
    """Formate une durée en heures en un texte lisible ('3h25', '1j 4h', ...)."""
    if hours is None:
        return "durée inconnue"
    total_minutes = int(round(hours * 60))
    days, rem = divmod(total_minutes, 24 * 60)
    hrs, mins = divmod(rem, 60)
    if days > 0:
        return f"{days}j {hrs}h"
    elif hrs > 0:
        return f"{hrs}h{mins:02d}"
    else:
        return f"{mins} min"


def compute_since_last(data, previous_state):
    """Calcule, pour chaque métrique, la variation depuis la dernière valeur
    sauvegardée (donc depuis le dernier message effectivement envoyé par le bot,
    quel que soit le temps réellement écoulé), ainsi que le score de risque
    précédent et la durée écoulée."""
    prev_values = previous_state.get("values")
    prev_timestamp = previous_state.get("timestamp")
    if not prev_values or not prev_timestamp:
        return None

    try:
        prev_dt = datetime.fromisoformat(prev_timestamp)
    except ValueError:
        return None

    now = datetime.now(paris)
    hours_elapsed = (now - prev_dt).total_seconds() / 3600

    since_last = {}
    for name, d in data.items():
        if not d:
            continue
        prev_val = prev_values.get(name)
        if prev_val is None or prev_val == 0:
            continue
        current_val = d["value"]
        change_since = round((current_val - prev_val) / prev_val * 100, 2)
        since_last[name] = {
            "value": current_val,
            "prev_value": prev_val,
            "change_since": change_since,
        }

    return {
        "hours_elapsed": hours_elapsed,
        "metrics": since_last,
        "prev_risk_score": previous_state.get("risk_score"),
    }


def generate_since_last_summary(data, since_last, risk_score, risk_label):
    """Synthèse équivalente à generate_natural_summary, mais calculée sur la
    variation réelle depuis le dernier message envoyé, pas sur un pas de 24h fixe."""
    metrics = since_last["metrics"]
    hours = since_last["hours_elapsed"]
    prev_score = since_last["prev_risk_score"]

    parts = [f"Comparaison avec le dernier envoi, il y a **{format_elapsed(hours)}**."]

    if risk_score is not None and prev_score is not None:
        delta_score = round(risk_score - prev_score, 1)
        arrow = "📈" if delta_score > 0 else ("📉" if delta_score < 0 else "➖")
        parts.append(
            f"{arrow} Score de risque composite : {prev_score} → **{risk_score}/100** "
            f"({delta_score:+.1f} pts) — {risk_label}."
        )
    elif risk_score is not None:
        parts.append(f"**Score de risque composite actuel : {risk_score}/100 — {risk_label}.**")

    vix = metrics.get("VIX")
    if vix:
        badge, word = get_change_badge(vix["change_since"])
        direction = "monté" if vix["change_since"] > 0 else ("baissé" if vix["change_since"] < 0 else "stagné")
        parts.append(
            f"Le VIX a {direction} de {vix['change_since']:+.2f}% depuis le dernier envoi "
            f"({vix['prev_value']} → {vix['value']}) — {badge} {word}."
        )

    vix9d = metrics.get("VIX9D")
    if vix9d and vix:
        if vix9d["change_since"] > vix["change_since"] + 3:
            parts.append(
                "⚡ Le VIX9D a accéléré nettement plus vite que le VIX depuis le dernier message, "
                "signe d'une inquiétude concentrée sur un événement proche plutôt qu'un stress généralisé."
            )

    vvix = metrics.get("VVIX")
    skew = metrics.get("SKEW")
    if vvix and skew:
        if vvix["change_since"] <= -3 and skew["change_since"] >= 3:
            parts.append(
                "⚠️ Depuis le dernier envoi, le VVIX s'est détendu pendant que le SKEW a progressé — "
                "la couverture contre un scénario extrême augmente sans hausse de la nervosité générale."
            )
        elif vvix["change_since"] >= 3 and skew["change_since"] >= 3:
            parts.append(
                "🔴 VVIX et SKEW ont tous deux progressé depuis le dernier envoi — instabilité anticipée "
                "et risque de mouvement extrême augmentent en même temps."
            )

    move = metrics.get("MOVE")
    if vix and move:
        vix_moved = abs(vix["change_since"]) >= 3
        move_moved = abs(move["change_since"]) >= 3
        if vix_moved and move_moved and (vix["change_since"] > 0) == (move["change_since"] > 0):
            direction_word = "tendu" if vix["change_since"] > 0 else "détendu"
            parts.append(f"🔴 Actions et taux se sont {direction_word}s de concert depuis le dernier envoi — mouvement cohérent, donc plus significatif.")
        elif move_moved and not vix_moved:
            parts.append("🟠 Le marché des taux a bougé sensiblement depuis le dernier envoi alors que les actions restent stables — à surveiller.")

    other_moves = []
    for name in ["VIX3M"]:
        m = metrics.get(name)
        if m and abs(m["change_since"]) >= 3:
            meaning = get_directional_meaning(name, m["change_since"])
            other_moves.append(f"**{name}** : {m['change_since']:+.2f}% depuis le dernier envoi — {meaning}")
    if other_moves:
        parts.append(" ".join(other_moves))

    return " ".join(parts)


def build_since_last_fields(since_last):
    fields = []
    order = ["VIX9D", "VIX", "VIX3M", "VVIX", "SKEW", "MOVE"]
    metrics = since_last["metrics"]
    for name in order:
        m = metrics.get(name)
        if not m:
            continue
        badge, word = get_change_badge(m["change_since"])
        directional_meaning = get_directional_meaning(name, m["change_since"])
        label = METRIC_INFO.get(name, {}).get("label", name)
        value_line = (
            f"**`{m['prev_value']}` → `{m['value']}`**\n"
            f"{badge} {word} ({m['change_since']:+.2f}% depuis le dernier envoi)\n"
            f"_{directional_meaning}_"
        )
        fields.append({
            "name": f"{name} — {label}",
            "value": value_line,
            "inline": False
        })
    return fields


# ====================== ENVOI DISCORD ======================

def build_24h_embed(data, risk_score, risk_label, contango_ratio, term_analysis, cross_asset, now_str):
    fields = []
    order = ["VIX9D", "VIX", "VIX3M", "VVIX", "SKEW", "MOVE"]

    for name in order:
        d = data.get(name)
        if not d:
            continue

        badge_1d, word_1d = get_change_badge(d["change_1d"])
        badge_5d, word_5d = get_change_badge(d["change_5d"])
        directional_meaning = get_directional_meaning(name, d["change_1d"])
        label = METRIC_INFO.get(name, {}).get("label", name)

        value_line = (
            f"**`{d['value']}`**\n"
            f"{badge_1d} {word_1d} (j: {d['change_1d']:+.2f}%) · {badge_5d} {word_5d} (5j: {d['change_5d']:+.2f}%)\n"
            f"📊 {d['pct_interpret']}\n"
            f"_{directional_meaning}_"
        )
        fields.append({
            "name": f"{name} — {label}",
            "value": value_line,
            "inline": False
        })

    summary = generate_natural_summary(data, contango_ratio, cross_asset, risk_score, risk_label)
    description = f"**{summary}**\n\n**Term Structure VIX :**\n{term_analysis}"

    if risk_score is None:
        color = 3447003
    elif risk_score < 40:
        color = 3066993
    elif risk_score < 70:
        color = 15844367
    else:
        color = 15158332

    return {
        "title": f"📊 Volatility Intelligence Report — {now_str}",
        "color": color,
        "fields": fields,
        "description": description,
        "footer": {"text": "Yahoo Finance • Z-score 20j = réactivité court terme • Percentile 1an = contexte annuel"}
    }, color


def build_since_last_embed(data, since_last, risk_score, risk_label, now_str, color):
    summary = generate_since_last_summary(data, since_last, risk_score, risk_label)
    fields = build_since_last_fields(since_last)
    return {
        "title": f"⏱️ Évolution depuis le dernier envoi — {now_str}",
        "color": color,
        "fields": fields,
        "description": f"**{summary}**",
        "footer": {"text": f"Comparaison avec l'état sauvegardé lors du dernier message du bot"}
    }


def post_to_discord(content, embed, username_suffix=""):
    payload = {
        "username": f"Volatilité Bot{username_suffix}",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/2920/2920277.png",
        "content": content,
        "embeds": [embed]
    }
    response = requests.post(WEBHOOK_URL, json=payload)
    print("Message envoyé" if response.status_code in [200, 204] else f"Erreur {response.status_code}: {response.text}")


def send_to_discord(data):
    now_str = datetime.now(paris).strftime("%d/%m/%Y à %H:%M")

    term_analysis, contango_ratio = analyze_vix_term_structure(data)
    cross_asset = analyze_cross_asset(data)
    risk_score, risk_label = compute_risk_score(data)

    previous_state = load_previous_state()
    alert = check_alert(risk_score, previous_state)
    since_last = compute_since_last(data, previous_state)

    # --- Message 1 : vue habituelle sur 24h ---
    embed_24h, color = build_24h_embed(data, risk_score, risk_label, contango_ratio, term_analysis, cross_asset, now_str)
    post_to_discord(alert if alert else None, embed_24h)

    # --- Message 2 : évolution depuis le dernier envoi effectif du bot ---
    if since_last is not None and since_last["metrics"]:
        embed_since_last = build_since_last_embed(data, since_last, risk_score, risk_label, now_str, color)
        post_to_discord(None, embed_since_last)
    else:
        print("Pas d'état précédent exploitable : message 'depuis le dernier envoi' ignoré (premier envoi ?).")

    save_state(risk_score, data)


if __name__ == "__main__":
    print("Récupération + analyse...")
    data = get_data()
    send_to_discord(data)
