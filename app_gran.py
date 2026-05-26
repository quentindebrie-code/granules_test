import streamlit as st

# ── Configuration ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Hympyr – Simulateur frais de livraison",
    page_icon="🌿",
    layout="centered",
)

# ── Données zones ─────────────────────────────────────────────────────────────
RAW = {
    1: ["31380","31660","81370","81800"],
    2: ["31130","31140","31180","31240","31340","31380","31590","31620",
        "81310","81500","81501","81502","81503","81506","81509","81600","81630"],
    3: ["31130","31131","31132","31133","31134","31135","31136","31137","31138","31139",
        "31140","31141","31142","31149","31150","31151","31152","31155","31159","31189",
        "31460","31570","31620","31621","31629","31790","81140","81150","81220","81300",
        "81301","81302","81303","81304","81305","81500","81600","81601","81602","81603",
        "81604","81605","81609","81630","82230","82370"],
    4: ["82000","82800","82170","81700","31840","31900","31901","31902","31903","31931","31945",
        "31947","31950","31957","31958","31960","31962","31998","31999","31650","31670",
        "31671","31672","31673","31674","31675","31676","31677","31678","31679","31681",
        "31682","31683","31685","31689","31692","31700","31701","31702","31703","31704",
        "31705","31706","31707","31708","31709","31711","31712","31715","31716","31750",
        "31780","31589","31489","31500","31503","31504","31505","31506","31507","31512",
        "31520","31200","31201","31203","31204","31205","31240","31241","31242","31243",
        "31244","31245","31249","31280","31289","31300","31312","31313","31314","31315",
        "31317","31319","31330","31389","31401","31402","31403","31404","31405","31406",
        "31432","31450","31000","31001","31002","31003","31004","31005","31006","31007",
        "31008","31009","31010","31011","31012","31013","31014","31015","31016","31017",
        "31018","31019","31020","31021","31022","31023","31024","31025","31026","31027",
        "31028","31029","31030","31031","31032","31033","31034","31035","31036","31037",
        "31038","31039","31040","31041","31042","31043","31044","31045","31046","31047",
        "31048","31049","31050","31051","31052","31053","31054","31055","31056","31057",
        "31058","31059","31060","31061","31062","31063","31064","31065","31066","31067",
        "31068","31069","31070","31071","31072","31073","31074","31075","31076","31077",
        "31078","31079","31080","31081","31082","31084","31085","31086","31088","31089",
        "31090","31091","31092","31093","31094","31095","31096","31097","31098","31099",
        "31101","31102","31103","31104","31106","31107","31109","31112","31170","31460"],
    5: ["31120","31121","31122","31123","31124","31125","31126","31127","31128","31129",
        "31270","31290","31320","31321","31322","31325","31326","31329","31490","31540",
        "31820","31830","31831","31832","31839","31860","31880","81170","81580","82140",
        "82290","82700","31250","31410","31470","31480","31530","31550","31560","31600",
        "31601","31602","31603","31604","31605","31606","31608","31609","31810","31870",
        "32430","32600","81090","81100","81101","81102","81103","81104","81105","81106",
        "81107","81108","81109","81110","81115","81116","81120","31131","81160","81190",
        "81210","81290","81350","81360","81380","81400","81430","81450","81540","81640",
        "81710","81990","82100","82130","82160","82220","82240","82250","82270","82300",
        "82301","82302","82303","82330","82440","82500","82600"],
}

TARIFS = {
    1: {"label": "≤ 10 km", "72h": 49, "15j": 29},
    2: {"label": "11 – 20 km", "72h": 49, "15j": 35},
    3: {"label": "21 – 30 km", "72h": 59, "15j": 45},
    4: {"label": "31 – 53 km", "72h": 79, "15j": 59},
    5: {"label": "54 – 60 km", "72h": 95, "15j": 69},
}

# Construire le dictionnaire CP → zone (priorité zone la plus proche)
CP_ZONE: dict[str, int] = {}
for zone in sorted(RAW.keys()):
    for cp in RAW[zone]:
        cp = cp.strip()
        if cp and cp not in CP_ZONE:
            CP_ZONE[cp] = zone

# ── CSS Hympyr ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap');

html, body, [class*="css"] { font-family: 'Poppins', sans-serif; }

/* Header */
.hympyr-header {
    background: linear-gradient(135deg, #005727 0%, #14B02F 100%);
    border-radius: 16px;
    padding: 28px 32px;
    margin-bottom: 32px;
    color: white;
}
.hympyr-header h1 { font-size: 1.6rem; font-weight: 700; margin: 0; }
.hympyr-header p  { margin: 6px 0 0; opacity: .85; font-size: .9rem; }

/* Résultat carte */
.result-card {
    border-radius: 14px;
    padding: 24px 28px;
    margin: 20px 0;
    border-left: 6px solid;
}
.zone-ok  { background: #f0faf3; border-color: #14B02F; }
.zone-err { background: #fff4f4; border-color: #e53935; }

.zone-badge {
    display: inline-block;
    background: #14B02F;
    color: white;
    font-size: 1rem;
    font-weight: 700;
    padding: 4px 14px;
    border-radius: 20px;
    margin-bottom: 12px;
}
.tarif-grid {
    display: flex;
    gap: 16px;
    margin-top: 14px;
    flex-wrap: wrap;
}
.tarif-box {
    flex: 1;
    min-width: 140px;
    background: white;
    border-radius: 10px;
    padding: 14px 18px;
    box-shadow: 0 2px 8px rgba(0,87,39,.10);
    text-align: center;
}
.tarif-box .label { font-size: .78rem; color: #666; margin-bottom: 4px; }
.tarif-box .price { font-size: 1.7rem; font-weight: 700; color: #005727; }
.tarif-box .sublabel { font-size: .75rem; color: #888; }

/* Grille complète */
.grid-table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: .88rem; }
.grid-table th { background: #005727; color: white; padding: 8px 12px; text-align: left; }
.grid-table td { padding: 8px 12px; border-bottom: 1px solid #e8f5e9; }
.grid-table tr:last-child td { border: none; }
.grid-table tr:hover td { background: #f0faf3; }
.z-badge {
    background: #e8f5e9; color: #005727; font-weight: 700;
    padding: 2px 10px; border-radius: 12px; font-size: .82rem;
}

/* Input */
div[data-testid="stTextInput"] input {
    font-size: 1.2rem !important;
    font-family: 'Poppins', sans-serif !important;
    border-radius: 10px !important;
    border: 2px solid #c8e6c9 !important;
    padding: 12px 16px !important;
    letter-spacing: 3px;
}
div[data-testid="stTextInput"] input:focus {
    border-color: #14B02F !important;
    box-shadow: 0 0 0 3px rgba(20,176,47,.15) !important;
}
</style>
""", unsafe_allow_html=True)

# ── En-tête ───────────────────────────────────────────────────────────────────
st.markdown("""
<div class="hympyr-header">
    <h1>🌿 Simulateur frais de livraison</h1>
    <p>Granulés de bois · Départ Saint-Sulpice (81370)</p>
</div>
""", unsafe_allow_html=True)

# ── Input ─────────────────────────────────────────────────────────────────────
cp_input = st.text_input(
    "Code postal du client",
    placeholder="ex : 81100",
    max_chars=5,
    label_visibility="visible",
)

# ── Résultat ──────────────────────────────────────────────────────────────────
if cp_input:
    cp = cp_input.strip().zfill(5)
    if not cp.isdigit() or len(cp) != 5:
        st.markdown('<div class="result-card zone-err">⚠️ Code postal invalide.</div>', unsafe_allow_html=True)
    elif cp in CP_ZONE:
        z = CP_ZONE[cp]
        t = TARIFS[z]
        st.markdown(f"""
        <div class="result-card zone-ok">
            <div class="zone-badge">Zone {z} — {t['label']} de Saint-Sulpice</div>
            <div style="color:#333; font-size:.9rem; margin-bottom:4px;">Code postal : <strong>{cp}</strong></div>
            <div class="tarif-grid">
                <div class="tarif-box">
                    <div class="label">⚡ Livraison sous 72H</div>
                    <div class="price">{t['72h']} €</div>
                    <div class="sublabel">prioritaire</div>
                </div>
                <div class="tarif-box">
                    <div class="label">📅 Livraison sous 15 jours</div>
                    <div class="price">{t['15j']} €</div>
                    <div class="sublabel">tournée optimisée</div>
                </div>
            </div>
            <div style="margin-top:14px; font-size:.78rem; color:#666;">
                ℹ️ À partir de 2 palettes, les frais de livraison sont facturés une seule fois.
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown(f"""
        <div class="result-card zone-err">
            <strong>❌ Code postal {cp} non référencé</strong><br>
            <span style="font-size:.88rem; color:#666; margin-top:6px; display:block;">
            Ce code postal ne fait pas partie des zones de livraison actuellement configurées.<br>
            Contactez-nous au 05 61 70 03 27 et nous étudierons votre demande dans les 24 heures.
            </span>
        </div>
        """, unsafe_allow_html=True)

# ── Grille tarifaire complète ─────────────────────────────────────────────────
st.markdown("---")
with st.expander("📋 Voir la grille tarifaire complète"):
    st.markdown("""
    <table class="grid-table">
        <tr>
            <th>Zone</th>
            <th>Rayon</th>
            <th>Sous 72H</th>
            <th>Sous 15 jours</th>
        </tr>
        <tr><td><span class="z-badge">Zone 1</span></td><td>≤ 10 km</td><td><strong>49 €</strong></td><td><strong>29 €</strong></td></tr>
        <tr><td><span class="z-badge">Zone 2</span></td><td>11 – 20 km</td><td><strong>49 €</strong></td><td><strong>35 €</strong></td></tr>
        <tr><td><span class="z-badge">Zone 3</span></td><td>21 – 30 km</td><td><strong>59 €</strong></td><td><strong>45 €</strong></td></tr>
        <tr><td><span class="z-badge">Zone 4</span></td><td>31 – 53 km</td><td><strong>79 €</strong></td><td><strong>59 €</strong></td></tr>
        <tr><td><span class="z-badge">Zone 5</span></td><td>54 – 60 km</td><td><strong>95 €</strong></td><td><strong>69 €</strong></td></tr>
    </table>
    <div style="font-size:.78rem; color:#888; margin-top:10px;">
    À partir de 2 palettes, les frais sont facturés une seule fois.
    </div>
    """, unsafe_allow_html=True)

st.markdown("""
<div style="text-align:center; color:#aaa; font-size:.75rem; margin-top:40px;">
Hympyr Energies · Usage interne équipe granulés
</div>
""", unsafe_allow_html=True)
