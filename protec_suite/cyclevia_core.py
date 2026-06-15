#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CyclEvia - collecte huile PROTEC - moteur sans interface"""

import csv
import io
import json
import os
import re
from datetime import datetime

try:
    from dateutil.relativedelta import relativedelta
    DATEUTIL_OK = True
except ImportError:
    DATEUTIL_OK = False

try:
    import requests as _req
    REQUESTS_OK = True
except ImportError:
    REQUESTS_OK = False
    import urllib.request as _urllib_req
    import urllib.error as _urllib_err

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config_cyclevia.json")

API_URL = "https://api.trackdechets.beta.gouv.fr"

DEFAULT_CONFIG = {
    "api_key": "",
    "siret_protec": "",
    "code_depot": "",
    "siret_osilub": "",
    "immatriculations_collecte": ["BN-860-KN", "EW-270-SZ", "EW167WS", "DK052FX", "EG-347-AM"],
    "immatriculations_actives": ["BN-860-KN"],
    "codes_huile": ["13 02 04*", "13 02 05*", "13 02 06*", "13 02 07*", "13 02 08*"],
    "categorie_defaut": "AUT",
    "mapping_categories": {},
    "mapping_type_huile": {
        "130204*": "HNM", "130205*": "HNM", "130206*": "HNM",
        "130207*": "HNM", "130208*": "HNM"
    }
}

QUERY = """query GetForms($siret:String!,$sentAfter:String,$status:[FormStatus!],
$first:Int,$cursorAfter:ID,$roles:[FormRole!]){
 forms(siret:$siret,sentAfter:$sentAfter,status:$status,first:$first,
       cursorAfter:$cursorAfter,roles:$roles){
  id readableId status sentAt receivedAt takenOverAt quantityReceived
  wasteDetails{code name quantity}
  emitter{type company{name siret address} workSite{name address city postalCode}}
  recipient{company{name siret address}}
  transporter{company{name siret} numberPlate}
  transporters{company{name siret} numberPlate}}}"""


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _gql(variables, api_key):
    payload = json.dumps({"query": QUERY, "variables": variables}).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    if REQUESTS_OK:
        r = _req.post(API_URL, data=payload, headers=headers, timeout=30)
        r.raise_for_status()
        d = r.json()
    else:
        req = _urllib_req.Request(API_URL, data=payload, headers=headers, method="POST")
        with _urllib_req.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    if "errors" in d:
        raise Exception("; ".join(e.get("message", str(e)) for e in d["errors"]))
    return d["data"]


def recuperer_bsds(siret, api_key, d0, d1, roles, logger=None):
    tous, cursor, page = [], None, 0
    while True:
        page += 1
        v = {
            "siret": siret,
            "sentAfter": d0.strftime("%Y-%m-%dT00:00:00.000Z"),
            "status": ["RECEIVED", "ACCEPTED", "PROCESSED", "FOLLOWED_WITH_PNTTD",
                       "AWAITING_GROUP", "GROUPED", "NO_TRACEABILITY", "SENT"],
            "first": 100, "roles": roles
        }
        if cursor:
            v["cursorAfter"] = cursor
        if logger:
            logger(f"   Page {page}...")
        bsds = _gql(v, api_key).get("forms", [])
        if logger:
            logger(f"   Page {page} : {len(bsds)} BSD")
        if not bsds:
            break
        tous.extend(bsds)
        if len(bsds) < 100:
            break
        cursor = bsds[-1]["id"]

    res = []
    for b in tous:
        dr = b.get("receivedAt") or b.get("takenOverAt") or b.get("sentAt")
        if dr:
            try:
                dt = datetime.fromisoformat(dr.replace("Z", "+00:00")).replace(tzinfo=None)
                if d0 <= dt < d1:
                    res.append(b)
            except ValueError:
                pass
    return res


def _norm_p(p):
    return p.upper().replace("-", "").replace(" ", "").replace("/", "") if p else ""


def _bsd_a_plaque(b, plaques):
    for tp in [b.get("transporter")] + (b.get("transporters") or []):
        if tp and tp.get("numberPlate"):
            for p in tp["numberPlate"].split(","):
                if _norm_p(p.strip()) in plaques:
                    return True
    return False


def filtrer_immat(bsds, immats):
    ps = {_norm_p(i) for i in immats}
    return [b for b in bsds if _bsd_a_plaque(b, ps)]


def _cp_ville(adr):
    if not adr:
        return "", ""
    m = re.search(r"(\d{5})\s+(.+?)$", adr.strip())
    if m:
        return m.group(1), m.group(2).upper().strip()
    return "", adr.upper().strip()


def _date_fr(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).replace(tzinfo=None).strftime("%d/%m/%Y")
    except ValueError:
        return ""


def _ced_compact(c):
    return c.replace(" ", "") if c else ""


def _qte(b):
    q = b.get("quantityReceived")
    if q is None:
        q = b.get("wasteDetails", {}).get("quantity", 0) or 0
    return float(q) if q else 0.0


def generer_csv_bytes(bsds, mois, annee, cfg):
    """Génère le CSV en mémoire, retourne (bytes_cp1252, nb_lignes, total_tonnes)."""
    cols = [
        "Code_depot", "date_enlevement", "numero_bon_enlevement", "siret_detenteur",
        "nom_detenteur", "code_postal_detenteur", "ville_detenteur", "categorie_detenteur",
        "type_collect", "code_ced", "type_huile", "type_pollution",
        "nature_collect_tonnage", "quantite", "nb_points_collect"
    ]
    code_depot = cfg.get("code_depot") or cfg.get("siret_protec", "")
    mcat = cfg.get("mapping_categories", {})
    mh = cfg.get("mapping_type_huile", {})
    cat_def = cfg.get("categorie_defaut", "AUT")

    lignes = []
    for b in bsds:
        em = b.get("emitter", {}) or {}
        co = em.get("company", {}) or {}
        ws = em.get("workSite") or {}
        siret_d = co.get("siret", "") or ""
        nom_d = (co.get("name", "") or "").upper()
        if ws.get("postalCode"):
            cp, v = ws["postalCode"], (ws.get("city") or "").upper()
        else:
            cp, v = _cp_ville(co.get("address", ""))
        code = _ced_compact(b.get("wasteDetails", {}).get("code", ""))
        cat = mcat.get(siret_d, cat_def)
        th = mh.get(code, "HNM")
        date_e = _date_fr(b.get("takenOverAt") or b.get("sentAt") or b.get("receivedAt"))
        num = re.sub(r"^BSD[D-]?", "", b.get("readableId", "") or "").lstrip("-")
        q = _qte(b)
        lignes.append({
            "Code_depot": f"'{code_depot}" if code_depot else "",
            "date_enlevement": date_e,
            "numero_bon_enlevement": num,
            "siret_detenteur": f"'{siret_d}" if siret_d else "",
            "nom_detenteur": nom_d,
            "code_postal_detenteur": f"'{cp}" if cp else "",
            "ville_detenteur": v,
            "categorie_detenteur": cat,
            "type_collect": "STA",
            "code_ced": code,
            "type_huile": th,
            "type_pollution": "NA",
            "nature_collect_tonnage": "T01",
            "quantite": (f"{q:.2f}".rstrip("0").rstrip(".") or "0"),
            "nb_points_collect": 1,
        })

    lignes.sort(key=lambda x: x["code_postal_detenteur"])

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, delimiter=";")
    w.writeheader()
    for l in lignes:
        w.writerow(l)

    csv_bytes = buf.getvalue().encode("cp1252", errors="replace")
    total = sum(float(l["quantite"] or 0) for l in lignes)
    return csv_bytes, len(lignes), round(total, 3)


def run_collecte(cfg, annee, mois, logger=None):
    """
    Récupère, filtre et génère le CSV CyclEvia pour un mois donné.
    Retourne (csv_bytes, nb_lignes, total_tonnes, filename).
    """
    if not DATEUTIL_OK:
        raise RuntimeError("python-dateutil requis : pip install python-dateutil")

    d0 = datetime(annee, mois, 1)
    d1 = d0 + relativedelta(months=1)

    noms = {
        1: "Janvier", 2: "Fevrier", 3: "Mars", 4: "Avril",
        5: "Mai", 6: "Juin", 7: "Juillet", 8: "Aout",
        9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Decembre"
    }
    filename = f"Collecte_{noms[mois]}_{annee}.csv"

    if logger:
        logger(f"Période : {mois:02d}/{annee}")
        logger(f"Plaques actives : {', '.join(cfg.get('immatriculations_actives', []))}")

    if logger:
        logger("1. Récupération BSD du mois (rôle RECIPIENT)...")
    bsds = recuperer_bsds(cfg["siret_protec"], cfg["api_key"], d0, d1, ["RECIPIENT"], logger)
    if logger:
        logger(f"   => {len(bsds)} BSD total")

    if logger:
        logger("2. Filtrage codes huile...")
    bsds = [b for b in bsds if b.get("wasteDetails", {}).get("code", "") in cfg["codes_huile"]]
    if logger:
        logger(f"   => {len(bsds)} BSD huile")

    if logger:
        logger("3. Filtrage immatriculations...")
    bsds = filtrer_immat(bsds, cfg.get("immatriculations_actives", []))
    if logger:
        logger(f"   => {len(bsds)} BSD après filtrage")

    if not bsds:
        if logger:
            logger("Aucun BSD pour ce mois.")
        return None, 0, 0.0, filename

    if logger:
        logger("4. Génération CSV...")
    csv_bytes, nb, total = generer_csv_bytes(bsds, mois, annee, cfg)
    if logger:
        logger(f"   => {nb} lignes, {total:.3f} t")
    return csv_bytes, nb, total, filename
