"""
Hympyr Énergies — Simulateur granulés de bois
Prix produit + frais de livraison + total TTC

Calcul des frais basé sur la DISTANCE ROUTIÈRE RÉELLE (et non plus le code postal).

Chaîne technique :
  1. Géocodage CP + ville  -> API Adresse (BAN), service public gratuit, sans clé
  2. Distance routière     -> OSRM public (gratuit, sans clé)
  3. Repli automatique     -> distance orthodromique majorée, puis saisie manuelle

Usage interne équipe commerciale.
"""

import math
from datetime import datetime

import requests
import streamlit as st

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION MÉTIER  —  tout ce qui change se modifie ICI
# ══════════════════════════════════════════════════════════════════════════════

ADRESSE_DEPART = "490 route de Toulouse, 81370 Saint-Sulpice-la-Pointe"

# Grille palettes : (borne supérieure incluse en km, libellé, prix 72h, prix 15j)
# Bornes CONTINUES pour éviter tout trou de grille avec des distances décimales.
ZONES_PALETTE = [
    (10.0, "Zone 1", "≤ 10 km", 49.0, 29.0),
    (20.0, "Zone 2", "> 10 à 20 km", 49.0, 35.0),
    (30.0, "Zone 3", "> 20 à 30 km", 59.0, 45.0),
    (53.0, "Zone 4", "> 30 à 53 km", 79.0, 59.0),
    (float("inf"), "Zone 5", "> 53 km", 95.0, 69.0),
]

# Vrac : franchise de 35 km, puis tarif au km au-delà
VRAC_FRANCHISE_KM = 35.0
VRAC_PRIX_KM = 1.40  # € TTC par km au-delà de la franchise

# Tarifs produits TTC
PRIX_PALETTES = {
    "Piveteau": 452.87,
    "Granulés de nos régions": 431.24,
}

# Vrac : prix TTC à la tonne selon le tonnage commandé
PRIX_VRAC_TONNE = {
    2: 437.0,
    3: 420.0,
    4: 415.0,
    5: 409.0,
    6: 404.0,
    7: 404.0,
    8: 399.0,
    9: 399.0,
}

# Seuil d'alerte : au-delà, la grille zone 5 est appliquée mais signalée
SEUIL_ALERTE_KM = 80.0

# Arrondi du kilométrage retenu pour la facturation : "entier" | "superieur" | "aucun"
ARRONDI_KM = "entier"

# Majoration appliquée à la distance à vol d'oiseau en cas de panne du routeur
COEF_VOL_OISEAU = 1.30

TELEPHONE = "05 61 70 03 27"

# ══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(
    page_title="Hympyr – Simulateur granulés",
    page_icon="🌿",
    layout="centered",
)


def eur(v: float) -> str:
    """Formate un montant en euros, format français."""
    s = f"{v:,.2f}".replace(",", "@").replace(".", ",").replace("@", "\u202f")
    return f"{s} €"


def km_fmt(v: float) -> str:
    return f"{v:.1f}".replace(".", ",") + " km"


def dec(v: float, n: int = 2) -> str:
    """Formate un nombre décimal au format français, sans symbole."""
    return f"{v:.{n}f}".replace(".", ",")


def arrondir_km(km: float) -> float:
    if ARRONDI_KM == "entier":
        return float(round(km))
    if ARRONDI_KM == "superieur":
        return float(math.ceil(km))
    return km


def haversine(lat1, lon1, lat2, lon2) -> float:
    """Distance orthodromique en km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ══════════════════════════════════════════════════════════════════════════════
# GÉOCODAGE ET ROUTAGE
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def geocoder_adresse(adresse: str):
    """Géocode une adresse libre via l'API Adresse (BAN). -> (lat, lon, label) ou None."""
    try:
        r = requests.get(
            "https://api-adresse.data.gouv.fr/search/",
            params={"q": adresse, "limit": 1},
            timeout=8,
        )
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            return None
        f = feats[0]
        lon, lat = f["geometry"]["coordinates"]
        return (lat, lon, f["properties"].get("label", adresse))
    except Exception:
        return None


@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def chercher_communes(code_postal: str, ville: str = ""):
    """
    Retourne la liste des communes correspondant au CP (et éventuellement à la ville).
    -> [{'label', 'ville', 'cp', 'lat', 'lon'}, ...]
    """
    params = {"limit": 15, "type": "municipality"}
    if ville.strip():
        params["q"] = ville.strip()
        params["postcode"] = code_postal
    else:
        params["q"] = code_postal
        params["postcode"] = code_postal

    try:
        r = requests.get(
            "https://api-adresse.data.gouv.fr/search/", params=params, timeout=8
        )
        r.raise_for_status()
        out = []
        for f in r.json().get("features", []):
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"]
            out.append(
                {
                    "label": p.get("label", ""),
                    "ville": p.get("city") or p.get("name", ""),
                    "cp": p.get("postcode", ""),
                    "lat": lat,
                    "lon": lon,
                }
            )
        return out
    except Exception:
        return []


@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def distance_routiere(lat1, lon1, lat2, lon2):
    """
    Distance routière réelle via OSRM.
    -> (km, minutes, source) ; source = 'osrm' | 'estimation'
    """
    try:
        url = (
            f"https://router.project-osrm.org/route/v1/driving/"
            f"{lon1},{lat1};{lon2},{lat2}"
        )
        r = requests.get(
            url, params={"overview": "false", "alternatives": "false"}, timeout=10
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            return (
                route["distance"] / 1000.0,
                route["duration"] / 60.0,
                "osrm",
            )
    except Exception:
        pass

    # Repli : orthodromique majorée (vitesse moyenne conventionnelle 55 km/h)
    km = haversine(lat1, lon1, lat2, lon2) * COEF_VOL_OISEAU
    return (km, km / 55.0 * 60.0, "estimation")


# ══════════════════════════════════════════════════════════════════════════════
# CALCULS TARIFAIRES
# ══════════════════════════════════════════════════════════════════════════════

def zone_palette(km: float):
    """-> (nom_zone, libelle, prix_72h, prix_15j)"""
    for borne, nom, libelle, p72, p15 in ZONES_PALETTE:
        if km <= borne:
            return nom, libelle, p72, p15
    return ZONES_PALETTE[-1][1:]


def frais_vrac(km: float) -> float:
    """1,40 € TTC par km au-delà de 35 km. 0 € en deçà."""
    excedent = max(0.0, km - VRAC_FRANCHISE_KM)
    return round(excedent * VRAC_PRIX_KM, 2)


def prix_vrac_tonne(tonnage: int) -> float:
    return PRIX_VRAC_TONNE[tonnage]


# ══════════════════════════════════════════════════════════════════════════════
# STYLE
# ══════════════════════════════════════════════════════════════════════════════

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');

html, body, [class*="css"] { font-family: 'Poppins', sans-serif; }

.hympyr-header {
    background: linear-gradient(135deg, #005727 0%, #14B02F 100%);
    border-radius: 16px;
    padding: 26px 30px;
    margin-bottom: 26px;
    color: white;
}
.hympyr-header h1 { font-size: 1.55rem; font-weight: 700; margin: 0; }
.hympyr-header p  { margin: 6px 0 0; opacity: .85; font-size: .88rem; }

.result-card {
    border-radius: 14px;
    padding: 22px 26px;
    margin: 18px 0;
    border-left: 6px solid;
    background: #f0faf3;
    border-color: #14B02F;
}
.card-warn { background: #fffaf0; border-color: #f0a202; }
.card-err  { background: #fff4f4; border-color: #e53935; }

.zone-badge {
    display: inline-block;
    background: #14B02F; color: white;
    font-size: .95rem; font-weight: 700;
    padding: 4px 14px; border-radius: 20px;
    margin-bottom: 10px;
}

.total-box {
    background: linear-gradient(135deg, #005727 0%, #14B02F 100%);
    color: white; border-radius: 14px;
    padding: 22px 26px; margin: 18px 0;
    text-align: center;
}
.total-box .lbl { font-size: .82rem; opacity: .85; letter-spacing: 1px; text-transform: uppercase; }
.total-box .val { font-size: 2.4rem; font-weight: 700; line-height: 1.2; }
.total-box .sub { font-size: .8rem; opacity: .8; }

.ligne {
    display: flex; justify-content: space-between;
    padding: 9px 0; border-bottom: 1px dashed #d7ece0;
    font-size: .95rem;
}
.ligne:last-child { border-bottom: none; }
.ligne .k { color: #444; }
.ligne .v { font-weight: 600; color: #005727; }

.grid-table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: .88rem; }
.grid-table th { background: #005727; color: white; padding: 8px 12px; text-align: left; }
.grid-table td { padding: 8px 12px; border-bottom: 1px solid #e8f5e9; }
.grid-table tr:last-child td { border: none; }
.z-badge { background: #e8f5e9; color: #005727; font-weight: 700;
           padding: 2px 10px; border-radius: 12px; font-size: .82rem; }
</style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
<div class="hympyr-header">
    <h1>🌿 Simulateur granulés — prix &amp; livraison</h1>
    <p>Départ : 490 route de Toulouse, 81370 Saint-Sulpice-la-Pointe · Distance routière réelle</p>
</div>
""",
    unsafe_allow_html=True,
)

# ══════════════════════════════════════════════════════════════════════════════
# PARAMÈTRES (barre latérale)
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("### ⚙️ Paramètres")
    adresse_depart = st.text_input("Adresse de départ", value=ADRESSE_DEPART)
    aller_retour = st.checkbox(
        "Compter l'aller-retour",
        value=False,
        help="Décoché : seul le trajet dépôt → client est facturé.",
    )
    st.divider()
    forcer_km = st.checkbox("Forcer le kilométrage manuellement", value=False)
    km_manuel = st.number_input(
        "Distance retenue (km)",
        min_value=0.0, max_value=500.0, value=0.0, step=1.0,
        disabled=not forcer_km,
    )
    st.divider()
    st.caption(
        "Distances calculées via l'API Adresse (data.gouv.fr) et OSRM. "
        "En cas d'indisponibilité, une estimation majorée est proposée : "
        "vérifier avant engagement client."
    )

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 1 — DESTINATION
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("#### 📍 1. Adresse de livraison")

c1, c2 = st.columns([1, 2])
with c1:
    cp = st.text_input("Code postal", placeholder="81100", max_chars=5)
with c2:
    ville = st.text_input("Ville (recommandé)", placeholder="Castres")

destination = None
km_reel = None
minutes = None
source = None

if cp and cp.strip().isdigit() and len(cp.strip()) == 5:
    communes = chercher_communes(cp.strip(), ville)

    if not communes:
        st.markdown(
            f"""<div class="result-card card-err">
            <strong>❌ Adresse introuvable</strong><br>
            <span style="font-size:.88rem;color:#666;">
            Aucune commune ne correspond au code postal {cp}
            {"et à la ville « " + ville + " »" if ville else ""}.
            Vérifiez la saisie, ou forcez le kilométrage dans le menu latéral.
            </span></div>""",
            unsafe_allow_html=True,
        )
    else:
        if len(communes) == 1:
            destination = communes[0]
            st.success(f"Destination : {destination['label']}")
        else:
            idx = st.selectbox(
                "Plusieurs communes correspondent — sélectionnez la bonne :",
                range(len(communes)),
                format_func=lambda i: communes[i]["label"],
            )
            destination = communes[idx]

# Calcul de la distance
if destination is not None:
    depart = geocoder_adresse(adresse_depart)
    if depart is None:
        st.error(
            "Impossible de géocoder l'adresse de départ. "
            "Vérifiez la connexion ou forcez le kilométrage manuellement."
        )
    else:
        with st.spinner("Calcul de l'itinéraire…"):
            km_reel, minutes, source = distance_routiere(
                depart[0], depart[1], destination["lat"], destination["lon"]
            )

# Kilométrage retenu
km_retenu = None
if forcer_km and km_manuel > 0:
    km_retenu = km_manuel
    source = "manuel"
elif km_reel is not None:
    km_retenu = km_reel * (2 if aller_retour else 1)

if km_retenu is not None:
    km_retenu = arrondir_km(km_retenu)

    libelle_source = {
        "osrm": "🛣️ Itinéraire routier réel",
        "estimation": "⚠️ Estimation (routeur indisponible) — à vérifier",
        "manuel": "✏️ Kilométrage saisi manuellement",
    }[source]

    trajet = "aller-retour" if aller_retour and source != "manuel" else "aller simple"
    duree = f" · ~{minutes:.0f} min" if minutes and source == "osrm" else ""

    st.markdown(
        f"""<div class="result-card {'card-warn' if source == 'estimation' else ''}">
        <div class="zone-badge">{km_fmt(km_retenu)} — {trajet}</div>
        <div style="font-size:.85rem;color:#555;">{libelle_source}{duree}</div>
        </div>""",
        unsafe_allow_html=True,
    )

    if km_retenu > SEUIL_ALERTE_KM:
        st.warning(
            f"Distance supérieure à {SEUIL_ALERTE_KM:.0f} km : "
            "la grille zone 5 s'applique, mais faites valider la faisabilité "
            "et la marge par l'exploitation avant engagement."
        )

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 2 — COMMANDE
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("#### 📦 2. Commande")

mode = st.radio(
    "Conditionnement",
    ["Palettes", "Vrac"],
    horizontal=True,
    label_visibility="collapsed",
)

prix_produit = 0.0
lignes = []
frais = None
detail_frais = ""

if mode == "Palettes":
    ca, cb = st.columns(2)
    with ca:
        nb_piveteau = st.number_input("Palettes Piveteau", 0, 20, 0, 1)
    with cb:
        nb_regions = st.number_input("Palettes Granulés de nos régions", 0, 20, 0, 1)

    delai = st.radio(
        "Délai de livraison",
        ["Sous 72 h (prioritaire)", "Sous 15 jours (tournée optimisée)"],
        horizontal=False,
    )

    nb_total = nb_piveteau + nb_regions

    if nb_piveteau:
        montant = nb_piveteau * PRIX_PALETTES["Piveteau"]
        prix_produit += montant
        lignes.append((f"{nb_piveteau} × Palette Piveteau", montant))
    if nb_regions:
        montant = nb_regions * PRIX_PALETTES["Granulés de nos régions"]
        prix_produit += montant
        lignes.append((f"{nb_regions} × Palette Granulés de nos régions", montant))

    if km_retenu is not None and nb_total > 0:
        nom_z, lib_z, p72, p15 = zone_palette(km_retenu)
        frais = p72 if delai.startswith("Sous 72") else p15
        detail_frais = (
            f"{nom_z} ({lib_z}) · "
            f"{'sous 72 h' if delai.startswith('Sous 72') else 'sous 15 jours'}"
            + (" · facturés une seule fois" if nb_total > 1 else "")
        )

else:  # Vrac
    tonnage = st.select_slider(
        "Tonnage commandé",
        options=list(PRIX_VRAC_TONNE.keys()),
        value=4,
        format_func=lambda t: f"{t} T",
    )
    pt = prix_vrac_tonne(tonnage)
    prix_produit = tonnage * pt
    lignes.append((f"{tonnage} T vrac × {eur(pt)}/T", prix_produit))

    if km_retenu is not None:
        frais = frais_vrac(km_retenu)
        excedent = max(0.0, km_retenu - VRAC_FRANCHISE_KM)
        if excedent == 0:
            detail_frais = f"Dans la franchise de {VRAC_FRANCHISE_KM:.0f} km — offerts"
        else:
            detail_frais = (
                f"{km_fmt(excedent)} au-delà de {VRAC_FRANCHISE_KM:.0f} km "
                f"× {dec(VRAC_PRIX_KM)} € TTC/km"
            )

# ══════════════════════════════════════════════════════════════════════════════
# ÉTAPE 3 — RÉSULTAT
# ══════════════════════════════════════════════════════════════════════════════

if prix_produit > 0:
    st.markdown("#### 🧮 3. Devis")

    html_lignes = "".join(
        f'<div class="ligne"><span class="k">{k}</span>'
        f'<span class="v">{eur(v)}</span></div>'
        for k, v in lignes
    )

    if frais is not None:
        html_lignes += (
            f'<div class="ligne"><span class="k">Frais de livraison<br>'
            f'<span style="font-size:.78rem;color:#888;">{detail_frais}</span></span>'
            f'<span class="v">{eur(frais)}</span></div>'
        )

    st.markdown(f'<div class="result-card">{html_lignes}</div>', unsafe_allow_html=True)

    if frais is None:
        st.info(
            "Saisissez le code postal (et la ville) pour obtenir les frais de "
            "livraison et le total."
        )
    else:
        total = prix_produit + frais
        st.markdown(
            f"""<div class="total-box">
            <div class="lbl">Total TTC</div>
            <div class="val">{eur(total)}</div>
            <div class="sub">dont {eur(prix_produit)} de marchandise
            et {eur(frais)} de livraison</div>
            </div>""",
            unsafe_allow_html=True,
        )

        # Récapitulatif copiable (mail / téléphone)
        dest_txt = destination["label"] if destination else f"{cp} {ville}".strip()
        recap = [
            f"Devis granulés — {datetime.now():%d/%m/%Y}",
            f"Livraison : {dest_txt}",
            f"Distance : {km_fmt(km_retenu)} ({'aller-retour' if aller_retour else 'aller simple'})",
            "",
        ]
        recap += [f"- {k} : {eur(v)}" for k, v in lignes]
        recap += [
            f"- Frais de livraison ({detail_frais}) : {eur(frais)}",
            "",
            f"TOTAL TTC : {eur(total)}",
        ]
        with st.expander("📋 Récapitulatif à copier (mail / téléphone)"):
            st.code("\n".join(recap), language=None)

# ══════════════════════════════════════════════════════════════════════════════
# GRILLES DE RÉFÉRENCE
# ══════════════════════════════════════════════════════════════════════════════

st.markdown("---")

with st.expander("📋 Grilles tarifaires de référence"):
    lignes_zones = "".join(
        f"<tr><td><span class='z-badge'>{nom}</span></td><td>{lib}</td>"
        f"<td><strong>{eur(p72)}</strong></td><td><strong>{eur(p15)}</strong></td></tr>"
        for _, nom, lib, p72, p15 in ZONES_PALETTE
    )
    lignes_vrac = "".join(
        f"<tr><td>{t} T</td><td><strong>{eur(p)}</strong> / tonne</td>"
        f"<td>{eur(t * p)}</td></tr>"
        for t, p in PRIX_VRAC_TONNE.items()
    )
    st.markdown(
        f"""
<strong style="color:#005727;">Livraison palettes</strong>
<table class="grid-table">
<tr><th>Zone</th><th>Distance routière</th><th>Sous 72 h</th><th>Sous 15 j</th></tr>
{lignes_zones}
</table>
<div style="font-size:.78rem;color:#888;margin:8px 0 18px;">
À partir de 2 palettes, les frais sont facturés une seule fois.
</div>

<strong style="color:#005727;">Livraison vrac</strong>
<div style="font-size:.88rem;margin:6px 0 18px;">
Gratuite jusqu'à {VRAC_FRANCHISE_KM:.0f} km, puis
{dec(VRAC_PRIX_KM)} € TTC par km au-delà.
</div>

<strong style="color:#005727;">Tarifs produits TTC</strong>
<table class="grid-table">
<tr><th>Palette</th><th>Prix TTC</th><th></th></tr>
<tr><td>Piveteau</td><td><strong>{eur(PRIX_PALETTES['Piveteau'])}</strong></td><td></td></tr>
<tr><td>Granulés de nos régions</td>
<td><strong>{eur(PRIX_PALETTES['Granulés de nos régions'])}</strong></td><td></td></tr>
</table>
<table class="grid-table" style="margin-top:14px;">
<tr><th>Vrac</th><th>Prix / tonne</th><th>Total marchandise</th></tr>
{lignes_vrac}
</table>
""",
        unsafe_allow_html=True,
    )

st.markdown(
    f"""
<div style="text-align:center;color:#aaa;font-size:.75rem;margin-top:36px;">
Hympyr Énergies · Usage interne équipe granulés · {TELEPHONE}<br>
Simulation indicative — ne vaut pas engagement contractuel.
</div>
""",
    unsafe_allow_html=True,
)
