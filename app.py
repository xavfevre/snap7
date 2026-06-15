#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilan kilométrique des déchets PROTEC — application web Flask
"""

import io
import json
import queue
import threading
import uuid
import datetime

from flask import Flask, Response, jsonify, render_template, request, send_file

from bilan_core import (
    GeoService,
    compute_bilan,
    export_excel,
    fetch_all_forms,
    load_config,
    parse_years,
    save_config,
)

app = Flask(__name__)

# job_id -> {"queue": Queue, "rows": list|None, "summary": dict|None,
#             "status": "running"|"done"|"error", "error": str|None}
_jobs: dict = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def post_config():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON invalide"}), 400
    save_config(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Lancement du calcul
# ---------------------------------------------------------------------------
@app.route("/api/run", methods=["POST"])
def run():
    data = request.get_json(force=True) or {}
    years_str = (data.get("years") or str(datetime.date.today().year)).strip()
    try:
        years = parse_years(years_str)
    except Exception:
        return jsonify({"error": "Format d'année invalide. Ex : 2025 ou 2023-2025"}), 400
    if not years:
        return jsonify({"error": "Indiquez au moins une année."}), 400

    cfg = load_config()
    if not cfg.get("td_api_token"):
        return jsonify({"error": "Jeton API Trackdéchets manquant (onglet Configuration)."}), 400
    if not cfg.get("protec_siret"):
        return jsonify({"error": "SIRET PROTEC manquant (onglet Configuration)."}), 400

    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    with _jobs_lock:
        _jobs[job_id] = {
            "queue": q,
            "rows": None,
            "summary": None,
            "status": "running",
            "error": None,
        }

    threading.Thread(target=_worker, args=(job_id, cfg, years, q), daemon=True).start()
    return jsonify({"job_id": job_id})


def _worker(job_id: str, cfg: dict, years: set, q: queue.Queue):
    def log(msg: str):
        q.put({"type": "log", "msg": msg})

    def progress(done: int, total: int):
        q.put({"type": "progress", "done": done, "total": total})

    try:
        log(f"\n=== Calcul pour {sorted(years)} ===")
        log("1) Récupération des BSDD Trackdéchets…")
        forms = fetch_all_forms(cfg["td_api_token"], cfg["protec_siret"], logger=log)
        log(f"   {len(forms)} BSDD au total.")

        log("2) Géocodage + calcul des itinéraires…")
        geo = GeoService(cfg.get("ors_api_key", ""), logger=log)

        rows, summary = compute_bilan(forms, cfg, geo, years, logger=log, progress=progress)

        with _jobs_lock:
            _jobs[job_id]["rows"] = rows
            _jobs[job_id]["summary"] = summary
            _jobs[job_id]["status"] = "done"

        log("\n3) Résultats (km comptés) :")
        agg: dict = {}
        for (yr, typ, cat, code), km in summary.items():
            agg[(yr, typ)] = round(agg.get((yr, typ), 0.0) + km, 2)
        for yr, typ in sorted(agg.keys()):
            log(f"   {yr}  {typ:<16} : {agg[(yr, typ)]:>12,.1f} km".replace(",", " "))
        total_km = round(sum(summary.values()), 1)
        log(f"   ----\n   TOTAL : {total_km:,.1f} km".replace(",", " "))
        log("\nTerminé. Vous pouvez exporter l'Excel.")

        summary_serializable = {
            f"{yr}|{typ}|{cat}|{code}": km
            for (yr, typ, cat, code), km in summary.items()
        }
        q.put({"type": "summary", "data": summary_serializable, "count_rows": len(rows)})
        q.put({"type": "done", "job_id": job_id})

    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)
        q.put({"type": "error", "msg": str(exc)})


# ---------------------------------------------------------------------------
# SSE : flux de progression
# ---------------------------------------------------------------------------
@app.route("/api/progress/<job_id>")
def progress_stream(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job introuvable"}), 404

    def generate():
        q = job["queue"]
        while True:
            try:
                event = q.get(timeout=30)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield 'data: {"type":"ping"}\n\n'

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Résultats JSON (tableau détail)
# ---------------------------------------------------------------------------
@app.route("/api/results/<job_id>")
def get_results(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job introuvable"}), 404
    if job["status"] != "done":
        return jsonify({"error": "calcul non terminé"}), 400

    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 100))
    rows = job["rows"] or []
    total = len(rows)
    start = (page - 1) * per_page
    end = start + per_page
    return jsonify({
        "rows": rows[start:end],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })


# ---------------------------------------------------------------------------
# Export Excel
# ---------------------------------------------------------------------------
@app.route("/api/export/<job_id>")
def export(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "job introuvable"}), 404
    if job["status"] != "done":
        return jsonify({"error": "calcul non terminé"}), 400

    cfg = load_config()
    buf = io.BytesIO()
    export_excel(job["rows"], job["summary"], buf, round_trip=bool(cfg.get("round_trip", True)))
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name="bilan_km_protec.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
