"""
Hympyr Énergies — Cockpit granulés de bois
==========================================

4 onglets :
  1. Simulateur      — prix produit + frais de livraison (distance routière réelle)
  2. Grille          — grille tarifaire datée, éditable, exportable PDF / Excel
  3. Données         — import de grilles historiques et de tarifs concurrents
  4. Analyse         — grilles de lecture des prix + rapport PDF / Excel

Chaîne technique distance :
  Géocodage CP+ville -> API Adresse (BAN, gratuit, sans clé)
  Distance routière  -> OSRM public (gratuit, sans clé)
  Repli              -> orthodromique majorée, puis saisie manuelle

Persistance : session uniquement (Streamlit Cloud a un système de fichiers
éphémère). L'archivage se fait par export/import Excel.

Usage interne équipe commerciale.
"""

import io
import math
import os
import re
import unicodedata
from datetime import date, datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import streamlit as st

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION MÉTIER — tout ce qui change se modifie ICI
# ══════════════════════════════════════════════════════════════════════════════

ADRESSE_DEPART = "490 route de Toulouse, 81370 Saint-Sulpice-la-Pointe"
TELEPHONE = "05 61 70 03 27"
SOCIETE = "Hympyr Énergies"

# Identité visuelle (centralisée : une seule ligne à changer si la charte évolue)
VERT_FONCE = "#005727"
VERT_VIF = "#14B02F"
VERT_CLAIR = "#f0faf3"
GRIS = "#666666"
ROUGE = "#e53935"
ORANGE = "#f0a202"

# Grille livraison palettes : bornes CONTINUES (pas de trou avec des décimales)
ZONES_DEFAUT = [
    {"Zone": "Zone 1", "Jusqu'à (km)": 10.0, "Sous 72 h": 49.0, "Sous 15 j": 29.0},
    {"Zone": "Zone 2", "Jusqu'à (km)": 20.0, "Sous 72 h": 49.0, "Sous 15 j": 35.0},
    {"Zone": "Zone 3", "Jusqu'à (km)": 30.0, "Sous 72 h": 59.0, "Sous 15 j": 45.0},
    {"Zone": "Zone 4", "Jusqu'à (km)": 53.0, "Sous 72 h": 79.0, "Sous 15 j": 59.0},
    {"Zone": "Zone 5", "Jusqu'à (km)": 9999.0, "Sous 72 h": 95.0, "Sous 15 j": 69.0},
]

VRAC_FRANCHISE_KM = 35.0
VRAC_PRIX_KM = 1.40  # € TTC / km au-delà de la franchise

PALETTES_DEFAUT = {
    "Piveteau": 452.87,
    "Granulés de nos régions": 431.24,
}

VRAC_DEFAUT = {  # tonnage -> € TTC / tonne
    2: 437.0,
    3: 420.0,
    4: 415.0,
    5: 409.0,
    6: 404.0,
    7: 404.0,
    8: 399.0,
    9: 399.0,
}

SEUIL_ALERTE_KM = 80.0
ARRONDI_KM = "entier"  # "entier" | "superieur" | "aucun"
COEF_VOL_OISEAU = 1.30

# Schéma canonique de l'historique de prix
COLONNES = [
    "date",            # date d'effet du tarif
    "acteur",          # "Hympyr" ou nom du concurrent
    "type_acteur",     # "Interne" | "Concurrent"
    "conditionnement", # "Palette" | "Vrac"
    "produit",         # libellé produit
    "palier_t",        # palier de tonnage (vrac), sinon vide
    "unite",           # "palette" | "tonne"
    "prix_ttc",        # € TTC
]

st.set_page_config(
    page_title="Hympyr – Cockpit granulés",
    page_icon="🌿",
    layout="wide",
)

# ══════════════════════════════════════════════════════════════════════════════
# ===== LOGIQUE PURE — DÉBUT (testable hors Streamlit) =========================
# ══════════════════════════════════════════════════════════════════════════════

def eur(v) -> str:
    """Montant en euros, format français."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    s = f"{v:,.2f}".replace(",", "@").replace(".", ",").replace("@", "\u202f")
    return f"{s} €"


def dec(v, n: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.{n}f}".replace(".", ",")


def km_fmt(v: float) -> str:
    return dec(v, 1) + " km"


def pct(v, signe: bool = True) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    s = "+" if (signe and v > 0) else ""
    return s + dec(v, 1) + " %"


def arrondir_km(km: float) -> float:
    if ARRONDI_KM == "entier":
        return float(round(km))
    if ARRONDI_KM == "superieur":
        return float(math.ceil(km))
    return km


def haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normaliser(txt) -> str:
    """Minuscules, sans accents ni ponctuation — pour l'appariement de colonnes."""
    t = unicodedata.normalize("NFKD", str(txt))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")


# ── Tarification ──────────────────────────────────────────────────────────────

def zone_palette(km: float, zones: list):
    """-> (nom_zone, libellé, prix_72h, prix_15j). Bornes continues, tri croissant."""
    tri = sorted(zones, key=lambda z: float(z["Jusqu'à (km)"]))
    precedent = 0.0
    for z in tri:
        borne = float(z["Jusqu'à (km)"])
        if km <= borne:
            if precedent == 0:
                lib = f"≤ {dec(borne, 0)} km"
            elif borne < 9999:
                lib = f"> {dec(precedent, 0)} à {dec(borne, 0)} km"
            else:
                lib = f"> {dec(precedent, 0)} km"
            return z["Zone"], lib, float(z["Sous 72 h"]), float(z["Sous 15 j"])
        precedent = borne
    d = tri[-1]
    return (d["Zone"], f"> {dec(precedent, 0)} km",
            float(d["Sous 72 h"]), float(d["Sous 15 j"]))


def frais_vrac(km: float, franchise: float = VRAC_FRANCHISE_KM,
               prix_km: float = VRAC_PRIX_KM) -> float:
    """Franchise puis tarif linéaire au km."""
    return round(max(0.0, km - franchise) * prix_km, 2)


def cout_livre(prix_produit: float, frais: float, tonnes: float) -> float:
    """Coût rendu à la tonne — le seul comparable réellement honnête."""
    if not tonnes or tonnes <= 0:
        return float("nan")
    return (prix_produit + frais) / tonnes


# ── Modèle de données ─────────────────────────────────────────────────────────

def df_vide() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in COLONNES})
    df["prix_ttc"] = pd.Series(dtype="float64")
    df["palier_t"] = pd.Series(dtype="float64")
    return df


def reference(row) -> str:
    """Clé produit unifiée : 'Piveteau', 'Vrac 4 T', ..."""
    if str(row["conditionnement"]).strip().lower().startswith("vrac"):
        p = row.get("palier_t")
        try:
            if p is None or (isinstance(p, float) and math.isnan(p)):
                return "Vrac"
            return f"Vrac {int(float(p))} T"
        except (TypeError, ValueError):
            return "Vrac"
    return str(row["produit"]).strip()


def enrichir(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute la colonne 'reference', normalise les types, écarte les lignes vides."""
    if df is None or df.empty:
        d = df_vide()
        d["reference"] = pd.Series(dtype="object")
        return d
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["prix_ttc"] = pd.to_numeric(d["prix_ttc"], errors="coerce")
    d["palier_t"] = pd.to_numeric(d["palier_t"], errors="coerce")
    d = d.dropna(subset=["date", "prix_ttc"])
    if d.empty:
        d = df_vide()
        d["reference"] = pd.Series(dtype="object")
        return d
    d["reference"] = d.apply(reference, axis=1)
    return d


def grille_vers_lignes(date_effet, palettes: dict, vrac: dict,
                       acteur: str = "Hympyr") -> pd.DataFrame:
    """Convertit une grille tarifaire en lignes canoniques."""
    lignes = []
    for nom, prix in palettes.items():
        lignes.append({
            "date": pd.Timestamp(date_effet), "acteur": acteur,
            "type_acteur": "Interne", "conditionnement": "Palette",
            "produit": nom, "palier_t": np.nan, "unite": "palette",
            "prix_ttc": float(prix),
        })
    for t, prix in vrac.items():
        lignes.append({
            "date": pd.Timestamp(date_effet), "acteur": acteur,
            "type_acteur": "Interne", "conditionnement": "Vrac",
            "produit": "Granulés vrac", "palier_t": float(t), "unite": "tonne",
            "prix_ttc": float(prix),
        })
    return pd.DataFrame(lignes, columns=COLONNES)


ALIAS = {
    "date": ["date", "date_effet", "date_d_effet", "periode", "mois", "jour"],
    "acteur": ["acteur", "fournisseur", "concurrent", "enseigne", "societe",
               "entreprise", "marque"],
    "type_acteur": ["type_acteur", "type", "categorie", "nature"],
    "conditionnement": ["conditionnement", "format", "type_produit", "packaging"],
    "produit": ["produit", "libelle", "designation", "article", "reference"],
    "palier_t": ["palier_t", "palier", "tonnage", "tonnes", "quantite_t", "qte"],
    "unite": ["unite", "unit"],
    "prix_ttc": ["prix_ttc", "prix", "tarif", "montant", "prix_ttc_eur",
                 "prix_unitaire", "prix_ttc_unitaire", "prix_tonne", "prix_palette"],
}


def mapper_colonnes(cols) -> dict:
    """Apparie les colonnes d'un fichier importé au schéma canonique."""
    norm = {}
    for c in cols:
        norm.setdefault(normaliser(c), c)
    mapping = {}
    for cible, alias in ALIAS.items():
        for a in alias:
            if a in norm:
                mapping[cible] = norm[a]
                break
    return mapping


def valider_import(brut: pd.DataFrame, nom_fichier: str = ""):
    """
    Normalise un fichier importé vers le schéma canonique.
    -> (df_valide, rejets, avertissements)
    """
    avertissements, rejets = [], []
    mapping = mapper_colonnes(brut.columns)

    manquantes = [c for c in ("date", "acteur", "prix_ttc") if c not in mapping]
    if manquantes:
        rejets.append(
            f"{nom_fichier} : colonnes obligatoires introuvables "
            f"({', '.join(manquantes)}). Fichier ignoré."
        )
        return df_vide(), rejets, avertissements

    d = pd.DataFrame(index=brut.index)
    for cible in COLONNES:
        d[cible] = brut[mapping[cible]] if cible in mapping else np.nan

    d["date"] = pd.to_datetime(d["date"], errors="coerce", dayfirst=True)
    d["prix_ttc"] = pd.to_numeric(
        d["prix_ttc"].astype(str)
        .str.replace("\u202f", "", regex=False)
        .str.replace("\u00a0", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace("€", "", regex=False)
        .str.replace(",", ".", regex=False),
        errors="coerce",
    )
    d["palier_t"] = pd.to_numeric(d["palier_t"], errors="coerce")

    n0 = len(d)
    invalides = d["date"].isna() | d["prix_ttc"].isna()
    if invalides.any():
        rejets.append(
            f"{nom_fichier} : {int(invalides.sum())} ligne(s) sur {n0} écartée(s) "
            "(date ou prix illisible)."
        )
    d = d[~invalides].copy()
    if d.empty:
        return df_vide(), rejets, avertissements

    # Valeurs déduites + traçabilité explicite des hypothèses
    if "conditionnement" not in mapping:
        d["conditionnement"] = np.where(d["palier_t"].notna(), "Vrac", "Palette")
        avertissements.append(
            f"{nom_fichier} : colonne « conditionnement » absente, déduite du palier.")
    d["conditionnement"] = (
        d["conditionnement"].astype(str).str.strip().str.capitalize()
        .replace({"Nan": "Palette", "": "Palette"}))

    d["acteur"] = d["acteur"].astype(str).str.strip()

    if "type_acteur" not in mapping:
        d["type_acteur"] = np.where(
            d["acteur"].str.lower().str.contains("hympyr"), "Interne", "Concurrent")
        avertissements.append(
            f"{nom_fichier} : colonne « type_acteur » absente, "
            "déduite du nom de l'acteur.")

    if "produit" not in mapping:
        d["produit"] = np.where(d["conditionnement"] == "Vrac",
                                "Granulés vrac", "Palette")
    if "unite" not in mapping:
        d["unite"] = np.where(d["conditionnement"] == "Vrac", "tonne", "palette")

    return d[COLONNES].reset_index(drop=True), rejets, avertissements


def dedoublonner(df: pd.DataFrame) -> pd.DataFrame:
    """Une seule ligne par (date, acteur, référence) — la dernière importée gagne."""
    if df is None or df.empty:
        return df_vide()
    d = enrichir(df)
    if d.empty:
        return df_vide()
    d = d.drop_duplicates(subset=["date", "acteur", "reference"], keep="last")
    return d[COLONNES].reset_index(drop=True)


# ── Grilles de lecture ────────────────────────────────────────────────────────

def matrice_dernier_prix(df: pd.DataFrame) -> pd.DataFrame:
    """Dernier prix connu par référence × acteur."""
    d = enrichir(df)
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values("date")
    dern = d.groupby(["reference", "acteur"], as_index=False).last()
    return dern.pivot(index="reference", columns="acteur", values="prix_ttc")


def positionnement(df: pd.DataFrame, interne: str = "Hympyr") -> pd.DataFrame:
    """
    Positionnement sur le dernier prix connu.
    Indice base 100 = moyenne des concurrents. < 100 : Hympyr moins cher.
    """
    m = matrice_dernier_prix(df)
    if m.empty or interne not in m.columns:
        return pd.DataFrame()
    concurrents = [c for c in m.columns if c != interne]
    if not concurrents:
        return pd.DataFrame()

    out = pd.DataFrame(index=m.index)
    out["Hympyr"] = m[interne]
    out["Moyenne marché"] = m[concurrents].mean(axis=1)
    out["Mini marché"] = m[concurrents].min(axis=1)
    out["Maxi marché"] = m[concurrents].max(axis=1)
    out["Écart € vs moyenne"] = out["Hympyr"] - out["Moyenne marché"]
    out["Écart % vs moyenne"] = (out["Hympyr"] / out["Moyenne marché"] - 1) * 100
    out["Indice (base 100)"] = out["Hympyr"] / out["Moyenne marché"] * 100

    rangs = []
    for i in m.index:
        serie = m.loc[i].dropna()
        rangs.append(int(serie.rank(method="min")[interne])
                     if interne in serie.index else np.nan)
    out["Rang"] = rangs
    out["Nb offres comparées"] = m.notna().sum(axis=1)
    return out.dropna(subset=["Hympyr", "Moyenne marché"])


def volatilite(df: pd.DataFrame) -> pd.DataFrame:
    """Dispersion des prix par référence sur toute la période."""
    d = enrichir(df)
    if d.empty:
        return pd.DataFrame()
    g = d.groupby("reference")["prix_ttc"]
    out = pd.DataFrame({
        "Nb relevés": g.count(),
        "Prix mini": g.min(),
        "Prix maxi": g.max(),
        "Prix moyen": g.mean(),
        "Écart-type": g.std(),
    })
    out["Amplitude %"] = (out["Prix maxi"] / out["Prix mini"] - 1) * 100
    return out.sort_values("Amplitude %", ascending=False)


def evolution_periode(df: pd.DataFrame, acteur: str = "Hympyr") -> pd.DataFrame:
    """Variation du premier au dernier relevé, par référence, pour un acteur."""
    d = enrichir(df)
    if d.empty:
        return pd.DataFrame()
    d = d[d["acteur"] == acteur]
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values("date")
    prem = d.groupby("reference").first()
    dern = d.groupby("reference").last()
    out = pd.DataFrame({
        "Première date": prem["date"].dt.date,
        "Prix initial": prem["prix_ttc"],
        "Dernière date": dern["date"].dt.date,
        "Prix actuel": dern["prix_ttc"],
    })
    out["Variation €"] = out["Prix actuel"] - out["Prix initial"]
    out["Variation %"] = (out["Prix actuel"] / out["Prix initial"] - 1) * 100
    return out.sort_values("Variation %", ascending=False)


def degressivite(df: pd.DataFrame) -> pd.DataFrame:
    """Prix/tonne par palier et par acteur (dernier prix connu)."""
    d = enrichir(df)
    if d.empty:
        return pd.DataFrame()
    d = d[d["conditionnement"].astype(str).str.lower().str.startswith("vrac")
          & d["palier_t"].notna()]
    if d.empty:
        return pd.DataFrame()
    d = d.sort_values("date").groupby(["acteur", "palier_t"], as_index=False).last()
    return d.pivot(index="palier_t", columns="acteur", values="prix_ttc").sort_index()


# ── Graphiques ────────────────────────────────────────────────────────────────

def _style_axes(ax, titre="", ylab=""):
    ax.set_title(titre, fontsize=11, fontweight="bold", color=VERT_FONCE, pad=12)
    ax.set_ylabel(ylab, fontsize=9)
    ax.grid(True, alpha=0.25, linestyle="--", linewidth=0.7)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(labelsize=8)


def fig_evolution(df: pd.DataFrame, ref: str):
    d = enrichir(df)
    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=140)
    d = d[d["reference"] == ref].sort_values("date") if not d.empty else d
    if d.empty:
        ax.text(0.5, 0.5, "Aucune donnée", ha="center", va="center")
        _style_axes(ax, f"Évolution du prix — {ref}", "€ TTC")
        return fig
    for acteur, sub in d.groupby("acteur"):
        interne = str(acteur).lower().startswith("hympyr")
        ax.plot(sub["date"], sub["prix_ttc"], marker="o", markersize=4,
                linewidth=2.4 if interne else 1.3,
                color=VERT_FONCE if interne else None,
                zorder=5 if interne else 2, label=acteur)
    _style_axes(ax, f"Évolution du prix — {ref}", "€ TTC")
    ax.legend(fontsize=7.5, frameon=False, ncol=3)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    return fig


def fig_vs_marche(df: pd.DataFrame, ref: str, interne: str = "Hympyr"):
    """Hympyr vs moyenne marché, avec bande mini-maxi des concurrents."""
    d = enrichir(df)
    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=140)
    d = d[d["reference"] == ref] if not d.empty else d
    if d.empty:
        ax.text(0.5, 0.5, "Aucune donnée", ha="center", va="center")
        _style_axes(ax, f"Positionnement vs marché — {ref}", "€ TTC")
        return fig

    conc = d[d["acteur"] != interne]
    if not conc.empty:
        g = conc.groupby("date")["prix_ttc"]
        stats = pd.DataFrame({"moy": g.mean(), "mini": g.min(), "maxi": g.max()})
        ax.fill_between(stats.index, stats["mini"], stats["maxi"],
                        color=VERT_VIF, alpha=0.13, label="Fourchette marché")
        ax.plot(stats.index, stats["moy"], linestyle="--", linewidth=1.6,
                color=GRIS, label="Moyenne marché")

    mine = d[d["acteur"] == interne].sort_values("date")
    if not mine.empty:
        ax.plot(mine["date"], mine["prix_ttc"], marker="o", markersize=5,
                linewidth=2.6, color=VERT_FONCE, label=interne, zorder=5)

    _style_axes(ax, f"Positionnement vs marché — {ref}", "€ TTC")
    ax.legend(fontsize=7.5, frameon=False)
    fig.autofmt_xdate(rotation=30)
    fig.tight_layout()
    return fig


def fig_degressivite(df: pd.DataFrame):
    piv = degressivite(df)
    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=140)
    if piv.empty:
        ax.text(0.5, 0.5, "Aucune donnée vrac", ha="center", va="center")
        _style_axes(ax, "Dégressivité vrac", "€ TTC / tonne")
        return fig
    for acteur in piv.columns:
        interne = str(acteur).lower().startswith("hympyr")
        ax.plot(piv.index, piv[acteur], marker="o", markersize=4,
                linewidth=2.4 if interne else 1.3,
                color=VERT_FONCE if interne else None,
                zorder=5 if interne else 2, label=acteur)
    _style_axes(ax, "Dégressivité vrac — prix à la tonne par palier",
                "€ TTC / tonne")
    ax.set_xlabel("Tonnage commandé", fontsize=9)
    ax.legend(fontsize=7.5, frameon=False, ncol=3)
    fig.tight_layout()
    return fig


def fig_positionnement(pos: pd.DataFrame):
    fig, ax = plt.subplots(figsize=(8, 3.8), dpi=140)
    if pos is None or pos.empty:
        ax.text(0.5, 0.5, "Aucun concurrent renseigné", ha="center", va="center")
        _style_axes(ax, "Écart vs moyenne marché", "")
        return fig
    d = pos.sort_values("Écart % vs moyenne")
    vals = d["Écart % vs moyenne"]
    couleurs = [ROUGE if v > 0 else VERT_VIF for v in vals]
    ax.barh([str(i) for i in d.index], vals, color=couleurs, height=0.6)
    ax.axvline(0, color=GRIS, linewidth=1)
    # Décalage des étiquettes proportionnel à l'échelle, sinon elles sortent
    # du cadre quand les écarts sont faibles.
    etendue = max(abs(float(vals.min())), abs(float(vals.max())), 0.1)
    marge = etendue * 0.06
    for i, v in enumerate(vals):
        ax.text(v + (marge if v >= 0 else -marge), i, pct(v), va="center",
                ha="left" if v >= 0 else "right", fontsize=7.5)
    ax.set_xlim(min(float(vals.min()) - etendue * 0.45, -etendue * 0.15),
                max(float(vals.max()) + etendue * 0.45, etendue * 0.15))
    _style_axes(ax, "Écart Hympyr vs moyenne marché (dernier prix)", "")
    ax.set_xlabel("% — négatif = Hympyr moins cher", fontsize=9)
    fig.tight_layout()
    return fig


def fig_to_png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ── Exports ───────────────────────────────────────────────────────────────────

def modele_import() -> pd.DataFrame:
    """Gabarit d'import à distribuer (colonnes attendues + exemples)."""
    return pd.DataFrame([
        {"date": "01/09/2026", "acteur": "Hympyr", "type_acteur": "Interne",
         "conditionnement": "Palette", "produit": "Piveteau", "palier_t": "",
         "unite": "palette", "prix_ttc": "452,87"},
        {"date": "01/09/2026", "acteur": "Concurrent A", "type_acteur": "Concurrent",
         "conditionnement": "Vrac", "produit": "Granulés vrac", "palier_t": "5",
         "unite": "tonne", "prix_ttc": "415,00"},
    ])


def _formater_classeur(book):
    """Mise en forme : en-têtes, largeurs, formats monétaires, volets figés."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    fill = PatternFill("solid", fgColor=VERT_FONCE.lstrip("#"))
    font = Font(color="FFFFFF", bold=True, size=10)

    for ws in book.worksheets:
        for cell in ws[1]:
            if cell.value is not None:
                cell.fill = fill
                cell.font = font
                cell.alignment = Alignment(horizontal="center", vertical="center",
                                           wrap_text=True)
        ws.freeze_panes = "A2"
        for col in ws.columns:
            lettre = get_column_letter(col[0].column)
            longueur = max((len(str(c.value)) for c in col if c.value is not None),
                           default=8)
            ws.column_dimensions[lettre].width = min(max(longueur + 3, 11), 34)

        entetes = [str(c.value or "") for c in ws[1]]
        for i, e in enumerate(entetes, start=1):
            n = normaliser(e)
            fmt = None
            if any(k in n for k in ("prix", "montant", "sous_", "moyenne",
                                    "mini", "maxi", "ecart")) and "%" not in e:
                fmt = '# ##0.00 "\u20ac"'
            if "%" in e or "indice" in n:
                fmt = "0.0"
            if fmt:
                for row in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    for c in row:
                        c.number_format = fmt


def export_excel(date_grille, palettes: dict, vrac: dict, zones: list,
                 historique: pd.DataFrame) -> bytes:
    """Classeur multi-onglets : grille datée + référentiel + données + analyses."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        lignes = [{"Conditionnement": "Palette", "Produit": k, "Palier (T)": "",
                   "Unité": "palette", "Prix TTC": v} for k, v in palettes.items()]
        lignes += [{"Conditionnement": "Vrac", "Produit": "Granulés vrac",
                    "Palier (T)": t, "Unité": "tonne", "Prix TTC": v}
                   for t, v in sorted(vrac.items())]
        g = pd.DataFrame(lignes)
        g.insert(0, "Date d'effet", pd.Timestamp(date_grille).date())
        g.to_excel(xw, sheet_name="Grille tarifaire", index=False)

        z = pd.DataFrame(zones)
        z.to_excel(xw, sheet_name="Livraison", index=False)
        pd.DataFrame([
            {"Règle vrac": "Franchise (km)", "Valeur": VRAC_FRANCHISE_KM},
            {"Règle vrac": "€ TTC / km au-delà", "Valeur": VRAC_PRIX_KM},
        ]).to_excel(xw, sheet_name="Livraison", index=False, startrow=len(z) + 3)

        h = enrichir(historique)
        if not h.empty:
            hh = h.copy()
            hh["date"] = hh["date"].dt.date
            hh.to_excel(xw, sheet_name="Données brutes", index=False)

            for nom, tab in (
                ("Matrice prix", matrice_dernier_prix(historique)),
                ("Positionnement", positionnement(historique)),
                ("Volatilité", volatilite(historique)),
                ("Évolution Hympyr", evolution_periode(historique)),
                ("Dégressivité vrac", degressivite(historique)),
            ):
                if tab is not None and not tab.empty:
                    tab.to_excel(xw, sheet_name=nom)

        modele_import().to_excel(xw, sheet_name="Modèle import", index=False)
        _formater_classeur(xw.book)
    return buf.getvalue()


class RapportPDF:
    """Rapport PDF à la charte Hympyr (fpdf2 + police Unicode embarquée)."""

    def __init__(self, titre: str, sous_titre: str = ""):
        from fpdf import FPDF

        self.pdf = FPDF(orientation="P", unit="mm", format="A4")
        self.pdf.set_auto_page_break(auto=True, margin=18)
        self._polices()
        self.titre = titre
        self.sous_titre = sous_titre
        self.pdf.add_page()
        self._bandeau()

    def _polices(self):
        """Poppins si présente dans assets/, sinon DejaVu (fournie par matplotlib)."""
        try:
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        except NameError:
            base = "assets"
        pr = os.path.join(base, "Poppins-Regular.ttf")
        pb = os.path.join(base, "Poppins-Bold.ttf")
        if os.path.exists(pr) and os.path.exists(pb):
            self.pdf.add_font("Marque", "", pr)
            self.pdf.add_font("Marque", "B", pb)
        else:
            d = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
            self.pdf.add_font("Marque", "", os.path.join(d, "DejaVuSans.ttf"))
            self.pdf.add_font("Marque", "B", os.path.join(d, "DejaVuSans-Bold.ttf"))
        self.pdf.set_font("Marque", "", 10)

    @staticmethod
    def _rgb(hexa):
        h = hexa.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    def _bandeau(self):
        p = self.pdf
        p.set_fill_color(*self._rgb(VERT_FONCE))
        p.rect(0, 0, 210, 30, "F")
        p.set_text_color(255, 255, 255)
        p.set_xy(14, 8)
        p.set_font("Marque", "B", 14)
        p.cell(0, 8, self.titre, new_x="LMARGIN", new_y="NEXT")
        p.set_x(14)
        p.set_font("Marque", "", 8.5)
        p.cell(0, 6, self.sous_titre, new_x="LMARGIN", new_y="NEXT")
        p.set_text_color(40, 40, 40)
        p.set_y(40)

    def titre_section(self, txt):
        p = self.pdf
        if p.get_y() > 245:
            p.add_page()
        p.ln(3)
        p.set_font("Marque", "B", 12)
        p.set_text_color(*self._rgb(VERT_FONCE))
        p.set_x(14)
        p.cell(0, 8, txt, new_x="LMARGIN", new_y="NEXT")
        p.set_draw_color(*self._rgb(VERT_VIF))
        p.set_line_width(0.6)
        y = p.get_y()
        p.line(14, y, 196, y)
        p.set_text_color(40, 40, 40)
        p.ln(4)

    def paragraphe(self, txt, taille=9.5):
        self.pdf.set_font("Marque", "", taille)
        self.pdf.set_x(14)
        self.pdf.multi_cell(182, 5, txt)
        self.pdf.ln(2)

    def _entete_tableau(self, cols, largeurs, taille):
        p = self.pdf
        p.set_font("Marque", "B", taille)
        p.set_fill_color(*self._rgb(VERT_FONCE))
        p.set_text_color(255, 255, 255)
        p.set_x(14)
        for c, w in zip(cols, largeurs):
            p.cell(w, 7, c[:30], border=0, align="C", fill=True)
        p.ln()
        p.set_text_color(40, 40, 40)
        p.set_font("Marque", "", taille)

    def tableau(self, df: pd.DataFrame, index_label: str = None,
                max_lignes: int = 28):
        p = self.pdf
        if df is None or df.empty:
            self.paragraphe("Aucune donnée disponible.")
            return

        d = df.head(max_lignes).copy()
        if index_label:
            d = d.reset_index()
            d = d.rename(columns={d.columns[0]: index_label})

        cols = [str(c) for c in d.columns]
        valeurs = [[self._fmt(v) for v in row] for row in d.itertuples(index=False)]

        poids = []
        for i, c in enumerate(cols):
            poids.append(max([len(c)] + [len(r[i]) for r in valeurs])
                         if valeurs else len(c))
        total = sum(poids) or 1
        largeurs = [max(15.0, 182 * w / total) for w in poids]
        facteur = 182 / sum(largeurs)
        largeurs = [w * facteur for w in largeurs]

        taille = 8 if len(cols) <= 6 else 6.6
        self._entete_tableau(cols, largeurs, taille)

        for i, row in enumerate(valeurs):
            if p.get_y() > 258:
                p.add_page()
                self._entete_tableau(cols, largeurs, taille)
            if i % 2 == 0:
                p.set_fill_color(240, 250, 243)
            else:
                p.set_fill_color(255, 255, 255)
            p.set_x(14)
            for v, w in zip(row, largeurs):
                p.cell(w, 6, v[:28], border=0, align="C", fill=True)
            p.ln()
        p.ln(3)

        if len(df) > max_lignes:
            self.paragraphe(
                f"({len(df) - max_lignes} ligne(s) non affichée(s) — "
                "voir l'export Excel.)", 8)

    @staticmethod
    def _fmt(v):
        if v is None:
            return "—"
        if isinstance(v, (pd.Timestamp, datetime, date)):
            return pd.Timestamp(v).strftime("%d/%m/%Y")
        if isinstance(v, (bool, np.bool_)):
            return "oui" if v else "non"
        if isinstance(v, (int, np.integer)):
            return str(int(v))
        if isinstance(v, (float, np.floating)):
            return "—" if math.isnan(float(v)) else dec(float(v), 2)
        return str(v)

    def image(self, png: bytes, largeur=182):
        p = self.pdf
        if p.get_y() > 195:
            p.add_page()
        p.image(io.BytesIO(png), x=14, w=largeur)
        p.ln(4)

    def pied(self, mentions):
        p = self.pdf
        p.ln(6)
        p.set_font("Marque", "", 7.5)
        p.set_text_color(130, 130, 130)
        p.set_x(14)
        p.multi_cell(182, 4, mentions)
        p.set_text_color(40, 40, 40)

    def bytes(self) -> bytes:
        return bytes(self.pdf.output())


def libelles_zones(zones: list) -> pd.DataFrame:
    zl, precedent = [], 0.0
    for z in sorted(zones, key=lambda x: float(x["Jusqu'à (km)"])):
        b = float(z["Jusqu'à (km)"])
        if precedent == 0:
            lib = f"≤ {dec(b, 0)} km"
        elif b < 9999:
            lib = f"> {dec(precedent, 0)} à {dec(b, 0)} km"
        else:
            lib = f"> {dec(precedent, 0)} km"
        zl.append({"Zone": z["Zone"], "Distance": lib,
                   "Sous 72 h (€)": float(z["Sous 72 h"]),
                   "Sous 15 j (€)": float(z["Sous 15 j"])})
        precedent = b
    return pd.DataFrame(zl)


def pdf_grille(date_grille, palettes: dict, vrac: dict, zones: list) -> bytes:
    r = RapportPDF(
        "Grille tarifaire granulés de bois",
        f"{SOCIETE} · Date d'effet : {pd.Timestamp(date_grille):%d/%m/%Y} · "
        f"Édité le {datetime.now():%d/%m/%Y}")

    r.titre_section("Granulés en palette")
    r.tableau(pd.DataFrame([{"Produit": k, "Prix TTC (€)": v}
                            for k, v in palettes.items()]))

    r.titre_section("Granulés en vrac")
    r.tableau(pd.DataFrame([
        {"Palier": f"{t} T", "Prix TTC / tonne (€)": v,
         "Total marchandise (€)": t * v} for t, v in sorted(vrac.items())]))

    r.titre_section("Frais de livraison — palettes")
    r.paragraphe(
        "Frais déterminés par la distance routière réelle entre le dépôt de "
        "Saint-Sulpice-la-Pointe et l'adresse de livraison. À partir de deux "
        "palettes, les frais sont facturés une seule fois.")
    r.tableau(libelles_zones(zones))

    r.titre_section("Frais de livraison — vrac")
    r.paragraphe(
        f"Livraison offerte jusqu'à {dec(VRAC_FRANCHISE_KM, 0)} km. Au-delà : "
        f"{dec(VRAC_PRIX_KM)} € TTC par kilomètre supplémentaire, calculé sur la "
        "distance routière réelle.")

    r.pied(f"{SOCIETE} · {TELEPHONE} · Document indicatif à usage interne. "
           "Prix TTC. Ne vaut pas offre contractuelle. Tarifs susceptibles de "
           "modification sans préavis.")
    return r.bytes()


def pdf_analyse(historique: pd.DataFrame, ref_focus: str = None) -> bytes:
    d = enrichir(historique)
    if d.empty:
        r = RapportPDF("Analyse tarifaire", SOCIETE)
        r.paragraphe("Aucune donnée à analyser.")
        return r.bytes()

    d1, d2 = d["date"].min(), d["date"].max()
    acteurs = sorted(d["acteur"].unique())
    concurrents = [a for a in acteurs if a.lower() != "hympyr"]

    r = RapportPDF(
        "Analyse tarifaire — granulés de bois",
        f"{SOCIETE} · Période {d1:%d/%m/%Y} → {d2:%d/%m/%Y} · "
        f"Édité le {datetime.now():%d/%m/%Y}")

    r.titre_section("Périmètre")
    r.paragraphe(
        f"{len(d)} relevés de prix · {len(acteurs)} acteur(s) dont "
        f"{len(concurrents)} concurrent(s) · {d['reference'].nunique()} référence(s) · "
        f"période du {d1:%d/%m/%Y} au {d2:%d/%m/%Y}.\n"
        f"Acteurs : {', '.join(acteurs)}.")

    pos = positionnement(historique)
    if not pos.empty:
        r.titre_section("Positionnement concurrentiel (dernier prix connu)")
        moy = pos["Écart % vs moyenne"].mean()
        sens = "en dessous de" if moy < 0 else "au-dessus de"
        r.paragraphe(
            f"Sur l'ensemble des références comparables, Hympyr se situe en moyenne "
            f"{dec(abs(moy), 1)} % {sens} la moyenne du marché. Indice moyen : "
            f"{dec(pos['Indice (base 100)'].mean(), 1)} (base 100 = marché).")
        r.tableau(pos[["Hympyr", "Moyenne marché", "Mini marché", "Maxi marché",
                       "Écart % vs moyenne", "Rang", "Nb offres comparées"]],
                  index_label="Référence")
        r.image(fig_to_png(fig_positionnement(pos)))

    ev = evolution_periode(historique)
    if not ev.empty and d["date"].nunique() > 1:
        r.titre_section("Évolution des prix Hympyr sur la période")
        r.tableau(ev, index_label="Référence")

    if ref_focus:
        r.titre_section(f"Focus référence — {ref_focus}")
        r.image(fig_to_png(fig_vs_marche(historique, ref_focus)))
        r.image(fig_to_png(fig_evolution(historique, ref_focus)))

    deg = degressivite(historique)
    if not deg.empty:
        r.titre_section("Dégressivité vrac")
        r.paragraphe("Comparaison des structures de dégressivité : une pente plus "
                     "forte signale une politique plus agressive sur les gros volumes.")
        r.image(fig_to_png(fig_degressivite(historique)))
        r.tableau(deg, index_label="Palier (T)")

    vol = volatilite(historique)
    if not vol.empty:
        r.titre_section("Volatilité par référence")
        r.paragraphe("Amplitude entre prix mini et maxi relevés sur la période, "
                     "tous acteurs confondus.")
        r.tableau(vol, index_label="Référence")

    r.pied(f"{SOCIETE} · {TELEPHONE} · Analyse fondée exclusivement sur les données "
           "importées par l'utilisateur ; leur exactitude et leur représentativité "
           "n'ont pas été vérifiées. Document interne.")
    return r.bytes()

# ══════════════════════════════════════════════════════════════════════════════
# ===== LOGIQUE PURE — FIN =====================================================
# ══════════════════════════════════════════════════════════════════════════════


# ── Géocodage / routage (cache Streamlit) ─────────────────────────────────────

@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def geocoder_adresse(adresse: str):
    try:
        r = requests.get("https://api-adresse.data.gouv.fr/search/",
                         params={"q": adresse, "limit": 1}, timeout=8)
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            return None
        lon, lat = feats[0]["geometry"]["coordinates"]
        return (lat, lon, feats[0]["properties"].get("label", adresse))
    except Exception:
        return None


@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def chercher_communes(code_postal: str, ville: str = ""):
    params = {"limit": 15, "type": "municipality"}
    if ville.strip():
        params["q"] = ville.strip()
        params["postcode"] = code_postal
    else:
        params["q"] = code_postal
        params["postcode"] = code_postal
    try:
        r = requests.get("https://api-adresse.data.gouv.fr/search/",
                         params=params, timeout=8)
        r.raise_for_status()
        out = []
        for f in r.json().get("features", []):
            p = f["properties"]
            lon, lat = f["geometry"]["coordinates"]
            out.append({"label": p.get("label", ""),
                        "ville": p.get("city") or p.get("name", ""),
                        "cp": p.get("postcode", ""), "lat": lat, "lon": lon})
        return out
    except Exception:
        return []


@st.cache_data(ttl=60 * 60 * 24 * 30, show_spinner=False)
def distance_routiere(lat1, lon1, lat2, lon2):
    try:
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{lon1},{lat1};{lon2},{lat2}")
        r = requests.get(url, params={"overview": "false", "alternatives": "false"},
                         timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("code") == "Ok" and data.get("routes"):
            route = data["routes"][0]
            return (route["distance"] / 1000.0, route["duration"] / 60.0, "osrm")
    except Exception:
        pass
    km = haversine(lat1, lon1, lat2, lon2) * COEF_VOL_OISEAU
    return (km, km / 55.0 * 60.0, "estimation")


# ══════════════════════════════════════════════════════════════════════════════
# ÉTAT DE SESSION
# ══════════════════════════════════════════════════════════════════════════════

def init_state():
    ss = st.session_state
    ss.setdefault("palettes", dict(PALETTES_DEFAUT))
    ss.setdefault("vrac", dict(VRAC_DEFAUT))
    ss.setdefault("zones", [dict(z) for z in ZONES_DEFAUT])
    ss.setdefault("date_grille", date.today())
    ss.setdefault("historique", df_vide())


init_state()

# ══════════════════════════════════════════════════════════════════════════════
# STYLE
# ══════════════════════════════════════════════════════════════════════════════

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');
html, body, [class*="css"] {{ font-family: 'Poppins', sans-serif; }}

.hympyr-header {{
    background: linear-gradient(135deg, {VERT_FONCE} 0%, {VERT_VIF} 100%);
    border-radius: 16px; padding: 24px 30px; margin-bottom: 20px; color: white;
}}
.hympyr-header h1 {{ font-size: 1.5rem; font-weight: 700; margin: 0; }}
.hympyr-header p  {{ margin: 6px 0 0; opacity: .85; font-size: .86rem; }}

.result-card {{
    border-radius: 14px; padding: 20px 24px; margin: 16px 0;
    border-left: 6px solid {VERT_VIF}; background: {VERT_CLAIR};
}}
.card-warn {{ background: #fffaf0; border-color: {ORANGE}; }}
.card-err  {{ background: #fff4f4; border-color: {ROUGE}; }}

.zone-badge {{
    display: inline-block; background: {VERT_VIF}; color: white;
    font-size: .95rem; font-weight: 700; padding: 4px 14px;
    border-radius: 20px; margin-bottom: 10px;
}}
.total-box {{
    background: linear-gradient(135deg, {VERT_FONCE} 0%, {VERT_VIF} 100%);
    color: white; border-radius: 14px; padding: 20px 24px;
    margin: 16px 0; text-align: center;
}}
.total-box .lbl {{ font-size: .8rem; opacity: .85; letter-spacing: 1px;
                   text-transform: uppercase; }}
.total-box .val {{ font-size: 2.3rem; font-weight: 700; line-height: 1.2; }}
.total-box .sub {{ font-size: .78rem; opacity: .8; }}

.ligne {{ display: flex; justify-content: space-between; padding: 9px 0;
          border-bottom: 1px dashed #d7ece0; font-size: .95rem; }}
.ligne:last-child {{ border-bottom: none; }}
.ligne .k {{ color: #444; }}
.ligne .v {{ font-weight: 600; color: {VERT_FONCE}; }}

div[data-testid="stTextInput"] input {{
    font-family: 'Poppins', sans-serif !important;
    border-radius: 10px !important; border: 2px solid #c8e6c9 !important;
}}
</style>
""", unsafe_allow_html=True)

st.markdown(f"""
<div class="hympyr-header">
    <h1>🌿 Cockpit granulés — {SOCIETE}</h1>
    <p>Départ : {ADRESSE_DEPART} · Distance routière réelle · Prix TTC</p>
</div>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# BARRE LATÉRALE
# ══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown("### ⚙️ Paramètres")
    adresse_depart = st.text_input("Adresse de départ", value=ADRESSE_DEPART)
    aller_retour = st.checkbox(
        "Compter l'aller-retour", value=False,
        help="Décoché : seul le trajet dépôt → client est facturé.")
    forcer_km = st.checkbox("Forcer le kilométrage", value=False)
    km_manuel = st.number_input("Distance retenue (km)", 0.0, 500.0, 0.0, 1.0,
                                disabled=not forcer_km)
    st.divider()
    _h = st.session_state["historique"]
    st.metric("Relevés de prix en session", len(_h))
    if not _h.empty:
        _d = pd.to_datetime(_h["date"], errors="coerce").dropna()
        if not _d.empty:
            st.caption(f"{_h['acteur'].nunique()} acteur(s) · "
                       f"{_d.min():%d/%m/%y} → {_d.max():%d/%m/%y}")
    st.caption("⚠️ Les données ne survivent pas à la fermeture de l'onglet. "
               "Exportez le classeur Excel pour archiver, réimportez-le pour reprendre.")

ong1, ong2, ong3, ong4 = st.tabs([
    "🧮 Simulateur", "📋 Grille tarifaire", "📥 Données", "📊 Analyse"])

# ══════════════════════════════════════════════════════════════════════════════
# ONGLET 1 — SIMULATEUR
# ══════════════════════════════════════════════════════════════════════════════

with ong1:
    st.markdown("#### 📍 Adresse de livraison")
    c1, c2 = st.columns([1, 2])
    with c1:
        cp = st.text_input("Code postal", placeholder="81100", max_chars=5)
    with c2:
        ville = st.text_input("Ville (recommandé)", placeholder="Castres")

    destination = km_reel = minutes = source = None

    if cp and cp.strip().isdigit() and len(cp.strip()) == 5:
        communes = chercher_communes(cp.strip(), ville)
        if not communes:
            st.markdown(
                f"""<div class="result-card card-err">
                <strong>❌ Adresse introuvable</strong><br>
                <span style="font-size:.88rem;color:#666;">
                Aucune commune ne correspond au code postal {cp}. Vérifiez la saisie
                ou forcez le kilométrage dans le menu latéral.</span></div>""",
                unsafe_allow_html=True)
        elif len(communes) == 1:
            destination = communes[0]
            st.success(f"Destination : {destination['label']}")
        else:
            idx = st.selectbox("Plusieurs communes correspondent :",
                               range(len(communes)),
                               format_func=lambda i: communes[i]["label"])
            destination = communes[idx]

    if destination is not None:
        depart = geocoder_adresse(adresse_depart)
        if depart is None:
            st.error("Impossible de géocoder l'adresse de départ. "
                     "Forcez le kilométrage manuellement.")
        else:
            with st.spinner("Calcul de l'itinéraire…"):
                km_reel, minutes, source = distance_routiere(
                    depart[0], depart[1], destination["lat"], destination["lon"])

    km_retenu = None
    if forcer_km and km_manuel > 0:
        km_retenu, source = km_manuel, "manuel"
    elif km_reel is not None:
        km_retenu = km_reel * (2 if aller_retour else 1)

    if km_retenu is not None:
        km_retenu = arrondir_km(km_retenu)
        lib_source = {
            "osrm": "🛣️ Itinéraire routier réel",
            "estimation": "⚠️ Estimation (routeur indisponible) — à vérifier",
            "manuel": "✏️ Kilométrage saisi manuellement",
        }[source]
        trajet = "aller-retour" if (aller_retour and source != "manuel") else "aller simple"
        duree = f" · ~{minutes:.0f} min" if (minutes and source == "osrm") else ""
        st.markdown(
            f"""<div class="result-card {'card-warn' if source == 'estimation' else ''}">
            <div class="zone-badge">{km_fmt(km_retenu)} — {trajet}</div>
            <div style="font-size:.85rem;color:#555;">{lib_source}{duree}</div>
            </div>""", unsafe_allow_html=True)

        if km_retenu > SEUIL_ALERTE_KM:
            st.warning(
                f"Distance supérieure à {SEUIL_ALERTE_KM:.0f} km : la grille zone 5 "
                "s'applique, mais faites valider faisabilité et marge par "
                "l'exploitation avant engagement.")

    st.markdown("#### 📦 Commande")
    mode = st.radio("Conditionnement", ["Palettes", "Vrac"], horizontal=True,
                    label_visibility="collapsed")

    prix_produit, lignes, frais, detail_frais, tonnes = 0.0, [], None, "", 0.0
    palettes = st.session_state["palettes"]
    vrac = st.session_state["vrac"]

    if mode == "Palettes":
        noms = list(palettes.keys())
        cols = st.columns(max(2, len(noms)))
        quantites = {}
        for nom, col in zip(noms, cols):
            with col:
                quantites[nom] = st.number_input(f"Palettes {nom}", 0, 20, 0, 1,
                                                 key=f"q_{nom}")
        delai = st.radio("Délai de livraison",
                         ["Sous 72 h (prioritaire)",
                          "Sous 15 jours (tournée optimisée)"])
        nb_total = sum(quantites.values())
        for nom, q in quantites.items():
            if q:
                montant = q * palettes[nom]
                prix_produit += montant
                lignes.append((f"{q} × Palette {nom}", montant))
        tonnes = float(nb_total)  # 1 palette ≈ 1 tonne
        if km_retenu is not None and nb_total > 0:
            nom_z, lib_z, p72, p15 = zone_palette(km_retenu, st.session_state["zones"])
            frais = p72 if delai.startswith("Sous 72") else p15
            detail_frais = (
                f"{nom_z} ({lib_z}) · "
                f"{'sous 72 h' if delai.startswith('Sous 72') else 'sous 15 jours'}"
                + (" · facturés une seule fois" if nb_total > 1 else ""))
    else:
        paliers = sorted(vrac.keys())
        tonnage = st.select_slider("Tonnage commandé", options=paliers,
                                   value=paliers[min(2, len(paliers) - 1)],
                                   format_func=lambda t: f"{t} T")
        pt = vrac[tonnage]
        prix_produit = tonnage * pt
        tonnes = float(tonnage)
        lignes.append((f"{tonnage} T vrac × {eur(pt)}/T", prix_produit))
        if km_retenu is not None:
            frais = frais_vrac(km_retenu)
            excedent = max(0.0, km_retenu - VRAC_FRANCHISE_KM)
            detail_frais = (
                f"Dans la franchise de {VRAC_FRANCHISE_KM:.0f} km — offerts"
                if excedent == 0 else
                f"{km_fmt(excedent)} au-delà de {VRAC_FRANCHISE_KM:.0f} km "
                f"× {dec(VRAC_PRIX_KM)} € TTC/km")

    if prix_produit > 0:
        st.markdown("#### 🧮 Devis")
        html = "".join(f'<div class="ligne"><span class="k">{k}</span>'
                       f'<span class="v">{eur(v)}</span></div>' for k, v in lignes)
        if frais is not None:
            html += (f'<div class="ligne"><span class="k">Frais de livraison<br>'
                     f'<span style="font-size:.78rem;color:#888;">{detail_frais}</span>'
                     f'</span><span class="v">{eur(frais)}</span></div>')
        st.markdown(f'<div class="result-card">{html}</div>', unsafe_allow_html=True)

        if frais is None:
            st.info("Saisissez le code postal pour obtenir les frais et le total.")
        else:
            total = prix_produit + frais
            st.markdown(
                f"""<div class="total-box">
                <div class="lbl">Total TTC</div>
                <div class="val">{eur(total)}</div>
                <div class="sub">dont {eur(prix_produit)} de marchandise et
                {eur(frais)} de livraison · coût rendu :
                {eur(cout_livre(prix_produit, frais, tonnes))} / tonne</div>
                </div>""", unsafe_allow_html=True)

            dest_txt = destination["label"] if destination else f"{cp} {ville}".strip()
            recap = [f"Devis granulés — {datetime.now():%d/%m/%Y}",
                     f"Livraison : {dest_txt}",
                     f"Distance : {km_fmt(km_retenu)} "
                     f"({'aller-retour' if aller_retour else 'aller simple'})", ""]
            recap += [f"- {k} : {eur(v)}" for k, v in lignes]
            recap += [f"- Frais de livraison ({detail_frais}) : {eur(frais)}", "",
                      f"TOTAL TTC : {eur(total)}"]
            with st.expander("📋 Récapitulatif à copier (mail / téléphone)"):
                st.code("\n".join(recap), language=None)

# ══════════════════════════════════════════════════════════════════════════════
# ONGLET 2 — GRILLE TARIFAIRE
# ══════════════════════════════════════════════════════════════════════════════

with ong2:
    st.markdown("#### 📋 Grille tarifaire en vigueur")
    st.caption("Les prix modifiés ici alimentent immédiatement le simulateur. "
               "Plus besoin de recoder lors d'une hausse fournisseur.")

    d_grille = st.date_input("Date d'effet de la grille",
                             value=st.session_state["date_grille"],
                             format="DD/MM/YYYY")
    st.session_state["date_grille"] = d_grille

    ca, cb = st.columns(2)
    with ca:
        st.markdown("**Palettes** — € TTC / palette")
        dfp = st.data_editor(
            pd.DataFrame([{"Produit": k, "Prix TTC": float(v)}
                          for k, v in st.session_state["palettes"].items()]),
            num_rows="dynamic", use_container_width=True, hide_index=True,
            key="ed_palettes")
    with cb:
        st.markdown("**Vrac** — € TTC / tonne par palier")
        dfv = st.data_editor(
            pd.DataFrame([{"Palier (T)": int(t), "Prix TTC / T": float(v)}
                          for t, v in sorted(st.session_state["vrac"].items())]),
            num_rows="dynamic", use_container_width=True, hide_index=True,
            key="ed_vrac")

    st.markdown("**Frais de livraison palettes** — bornes continues, en km")
    dfz = st.data_editor(pd.DataFrame(st.session_state["zones"]),
                         num_rows="dynamic", use_container_width=True,
                         hide_index=True, key="ed_zones")
    st.caption(f"Vrac : franchise {dec(VRAC_FRANCHISE_KM, 0)} km, puis "
               f"{dec(VRAC_PRIX_KM)} € TTC/km (constantes en tête de fichier).")

    if st.button("💾 Appliquer les modifications", type="primary"):
        try:
            np_ = {str(r["Produit"]).strip(): float(r["Prix TTC"])
                   for _, r in dfp.dropna().iterrows() if str(r["Produit"]).strip()}
            nv_ = {int(r["Palier (T)"]): float(r["Prix TTC / T"])
                   for _, r in dfv.dropna().iterrows()}
            nz_ = dfz.dropna().to_dict("records")
            if not np_ or not nv_ or not nz_:
                st.error("Chaque tableau doit contenir au moins une ligne valide.")
            else:
                st.session_state["palettes"] = np_
                st.session_state["vrac"] = nv_
                st.session_state["zones"] = nz_
                st.success("Grille mise à jour. Le simulateur utilise ces valeurs.")
        except Exception as e:
            st.error(f"Saisie invalide : {e}")

    st.divider()
    e1, e2, e3 = st.columns(3)
    with e1:
        st.download_button(
            "📄 Exporter la grille (PDF)",
            data=pdf_grille(d_grille, st.session_state["palettes"],
                            st.session_state["vrac"], st.session_state["zones"]),
            file_name=f"HYM_grille_granules_{pd.Timestamp(d_grille):%Y%m%d}.pdf",
            mime="application/pdf", use_container_width=True)
    with e2:
        st.download_button(
            "📊 Exporter le classeur (Excel)",
            data=export_excel(d_grille, st.session_state["palettes"],
                              st.session_state["vrac"], st.session_state["zones"],
                              st.session_state["historique"]),
            file_name=f"HYM_grille_granules_{pd.Timestamp(d_grille):%Y%m%d}.xlsx",
            mime=("application/vnd.openxmlformats-officedocument"
                  ".spreadsheetml.sheet"),
            use_container_width=True)
    with e3:
        if st.button("📌 Archiver cette grille dans l'historique",
                     use_container_width=True):
            nouv = grille_vers_lignes(d_grille, st.session_state["palettes"],
                                      st.session_state["vrac"])
            st.session_state["historique"] = dedoublonner(
                pd.concat([st.session_state["historique"], nouv], ignore_index=True))
            st.success(f"{len(nouv)} lignes archivées au "
                       f"{pd.Timestamp(d_grille):%d/%m/%Y}.")

# ══════════════════════════════════════════════════════════════════════════════
# ONGLET 3 — DONNÉES
# ══════════════════════════════════════════════════════════════════════════════

with ong3:
    st.markdown("#### 📥 Import de grilles tarifaires")
    st.caption("Formats acceptés : .xlsx, .csv — plusieurs fichiers à la fois. "
               "Colonnes minimales : date, acteur, prix. Les autres sont déduites.")

    fichiers = st.file_uploader("Fichiers à importer", type=["xlsx", "xls", "csv"],
                                accept_multiple_files=True)

    if fichiers and st.button("⬆️ Importer", type="primary"):
        total_ok, rejets, avert = 0, [], []
        for f in fichiers:
            try:
                if f.name.lower().endswith(".csv"):
                    brut = pd.read_csv(f, sep=None, engine="python")
                else:
                    brut = pd.read_excel(f)
            except Exception as e:
                rejets.append(f"{f.name} : illisible ({e}).")
                continue
            ok, rj, av = valider_import(brut, f.name)
            rejets += rj
            avert += av
            if not ok.empty:
                st.session_state["historique"] = pd.concat(
                    [st.session_state["historique"], ok], ignore_index=True)
                total_ok += len(ok)

        if total_ok:
            st.session_state["historique"] = dedoublonner(
                st.session_state["historique"])
            st.success(f"{total_ok} ligne(s) importée(s).")
        for a in avert:
            st.info("ℹ️ " + a)
        for r in rejets:
            st.warning("⚠️ " + r)

    with st.expander("📎 Modèle d'import à distribuer"):
        st.dataframe(modele_import(), use_container_width=True, hide_index=True)
        _buf = io.BytesIO()
        with pd.ExcelWriter(_buf, engine="openpyxl") as _xw:
            modele_import().to_excel(_xw, sheet_name="Modèle import", index=False)
        st.download_button("Télécharger le modèle (Excel)", data=_buf.getvalue(),
                           file_name="HYM_modele_import_prix.xlsx",
                           mime=("application/vnd.openxmlformats-officedocument"
                                 ".spreadsheetml.sheet"))

    st.divider()
    st.markdown("#### ✍️ Saisie directe d'un relevé concurrent")
    st.caption("Pour un prix relevé au téléphone ou sur un site, sans passer "
               "par un fichier.")

    with st.form("saisie_conc"):
        f1, f2, f3 = st.columns(3)
        with f1:
            s_date = st.date_input("Date du relevé", value=date.today(),
                                   format="DD/MM/YYYY")
            s_acteur = st.text_input("Concurrent", placeholder="ex : Péchavy")
        with f2:
            s_cond = st.selectbox("Conditionnement", ["Palette", "Vrac"])
            s_produit = st.text_input("Produit", value="Granulés")
        with f3:
            s_palier = st.number_input("Palier (T) — vrac uniquement", 0, 30, 0, 1)
            s_prix = st.number_input("Prix TTC", 0.0, 5000.0, 0.0, 0.01)
        if st.form_submit_button("Ajouter le relevé", type="primary"):
            if not s_acteur.strip() or s_prix <= 0:
                st.error("Nom du concurrent et prix strictement positif obligatoires.")
            else:
                ligne = pd.DataFrame([{
                    "date": pd.Timestamp(s_date), "acteur": s_acteur.strip(),
                    "type_acteur": "Concurrent", "conditionnement": s_cond,
                    "produit": s_produit.strip() or "Granulés",
                    "palier_t": (float(s_palier)
                                 if (s_cond == "Vrac" and s_palier) else np.nan),
                    "unite": "tonne" if s_cond == "Vrac" else "palette",
                    "prix_ttc": float(s_prix)}], columns=COLONNES)
                st.session_state["historique"] = dedoublonner(
                    pd.concat([st.session_state["historique"], ligne],
                              ignore_index=True))
                st.success(f"Relevé {s_acteur} ajouté.")

    st.divider()
    st.markdown("#### 🗂️ Données en session")
    hist = st.session_state["historique"]
    if hist.empty:
        st.info("Aucune donnée. Importez un fichier, saisissez un relevé, ou "
                "archivez la grille depuis l'onglet « Grille tarifaire ».")
    else:
        aff = enrichir(hist).copy()
        aff["date"] = aff["date"].dt.strftime("%d/%m/%Y")
        st.dataframe(aff, use_container_width=True, hide_index=True, height=340)
        if st.button("🗑️ Vider les données"):
            st.session_state["historique"] = df_vide()
            st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# ONGLET 4 — ANALYSE
# ══════════════════════════════════════════════════════════════════════════════

with ong4:
    hist = st.session_state["historique"]
    d = enrichir(hist)
    if d.empty:
        st.info("Aucune donnée à analyser. Rendez-vous dans l'onglet « Données ».")
    else:
        acteurs = sorted(d["acteur"].unique())
        concurrents = [a for a in acteurs if a.lower() != "hympyr"]
        refs = sorted(d["reference"].unique())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Relevés", len(d))
        m2.metric("Acteurs", len(acteurs))
        m3.metric("Références", len(refs))
        m4.metric("Période", f"{d['date'].min():%m/%y} → {d['date'].max():%m/%y}")

        if not concurrents:
            st.warning("Aucun concurrent renseigné : les lectures concurrentielles "
                       "resteront vides. Ajoutez des relevés dans l'onglet « Données ».")

        st.divider()
        st.markdown("##### 1. Matrice des prix — dernier prix connu")
        st.caption("Lecture transversale : qui pratique quoi, sur quelle référence.")
        mat = matrice_dernier_prix(hist)
        if mat.empty:
            st.info("Données insuffisantes.")
        else:
            sty = mat.style.format(lambda v: dec(v) if pd.notna(v) else "—")
            if mat.shape[1] > 1:
                sty = sty.background_gradient(cmap="RdYlGn_r", axis=1)
            st.dataframe(sty, use_container_width=True)

        st.divider()
        st.markdown("##### 2. Positionnement concurrentiel")
        st.caption("Indice base 100 = moyenne du marché. Inférieur à 100 : "
                   "Hympyr est moins cher.")
        pos = positionnement(hist)
        if pos.empty:
            st.info("Nécessite au moins un concurrent sur une référence commune "
                    "avec Hympyr.")
        else:
            st.dataframe(pos.style.format({
                "Hympyr": dec, "Moyenne marché": dec, "Mini marché": dec,
                "Maxi marché": dec, "Écart € vs moyenne": dec,
                "Écart % vs moyenne": pct,
                "Indice (base 100)": lambda v: dec(v, 1)}),
                use_container_width=True)
            st.pyplot(fig_positionnement(pos))
            moy = pos["Écart % vs moyenne"].mean()
            if moy < -3:
                st.success(f"Hympyr est en moyenne {dec(abs(moy), 1)} % sous le "
                           "marché. Vérifier que cet écart est un choix assumé, "
                           "pas une érosion de marge.")
            elif moy > 3:
                st.warning(f"Hympyr est en moyenne {dec(moy, 1)} % au-dessus du "
                           "marché. L'écart doit être argumenté par les commerciaux "
                           "(service, délai, proximité).")
            else:
                st.info(f"Hympyr est aligné sur le marché ({pct(moy)}).")

        st.divider()
        st.markdown("##### 3. Évolution dans le temps")
        ref_focus = st.selectbox("Référence à examiner", refs)
        g1, g2 = st.columns(2)
        with g1:
            st.pyplot(fig_evolution(hist, ref_focus))
        with g2:
            st.pyplot(fig_vs_marche(hist, ref_focus))

        ev = evolution_periode(hist)
        if not ev.empty:
            st.markdown("**Variation des prix Hympyr sur la période**")
            st.dataframe(ev.style.format({
                "Prix initial": dec, "Prix actuel": dec,
                "Variation €": dec, "Variation %": pct}),
                use_container_width=True)

        st.divider()
        st.markdown("##### 4. Dégressivité vrac")
        st.caption("Une pente plus forte traduit une politique plus agressive "
                   "sur les gros volumes.")
        deg = degressivite(hist)
        if deg.empty:
            st.info("Aucune donnée vrac avec palier renseigné.")
        else:
            st.pyplot(fig_degressivite(hist))
            st.dataframe(deg.style.format(lambda v: dec(v) if pd.notna(v) else "—"),
                         use_container_width=True)

        st.divider()
        st.markdown("##### 5. Volatilité par référence")
        vol = volatilite(hist)
        st.dataframe(vol.style.format({
            "Prix mini": dec, "Prix maxi": dec, "Prix moyen": dec,
            "Écart-type": dec, "Amplitude %": lambda v: pct(v, signe=False)}),
            use_container_width=True)

        st.divider()
        st.markdown("##### 📤 Export du rapport")
        r1, r2 = st.columns(2)
        with r1:
            st.download_button(
                "📄 Rapport d'analyse (PDF)",
                data=pdf_analyse(hist, ref_focus),
                file_name=f"HYM_analyse_prix_{datetime.now():%Y%m%d}.pdf",
                mime="application/pdf", use_container_width=True)
        with r2:
            st.download_button(
                "📊 Classeur complet (Excel)",
                data=export_excel(st.session_state["date_grille"],
                                  st.session_state["palettes"],
                                  st.session_state["vrac"],
                                  st.session_state["zones"], hist),
                file_name=f"HYM_analyse_prix_{datetime.now():%Y%m%d}.xlsx",
                mime=("application/vnd.openxmlformats-officedocument"
                      ".spreadsheetml.sheet"),
                use_container_width=True)

st.markdown(f"""
<div style="text-align:center;color:#aaa;font-size:.75rem;margin-top:30px;">
{SOCIETE} · Usage interne équipe granulés · {TELEPHONE}<br>
Simulations et analyses indicatives — ne valent pas engagement contractuel.
</div>
""", unsafe_allow_html=True)
