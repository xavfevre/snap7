#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PROTEC Suite - Application web unifiée"""

import io
import json
import os
import queue
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

import bilan_core
import cyclevia_core
import plc_core

APP_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["JSON_ENSURE_ASCII"] = False

_jobs = {}  # job_id → {queue, rows, summary, status, error, csv_bytes, ...}


# ---------------------------------------------------------------------------
# Utils SSE
# ---------------------------------------------------------------------------
def _sse(event_type, data):
    payload = json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data
    return f"event: {event_type}\ndata: {payload}\n\n"


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return app.send_static_file("index.html") if False else \
        open(os.path.join(APP_DIR, "templates", "index.html"), encoding="utf-8").read(), \
        200, {"Content-Type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------------------
# Config globale
# ---------------------------------------------------------------------------
@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify({
        "bilan": bilan_core.load_config(),
        "cyclevia": cyclevia_core.load_config(),
        "plc": plc_core.load_config(),
    })


@app.route("/api/config/bilan", methods=["POST"])
def save_bilan_config():
    cfg = request.get_json(force=True)
    bilan_core.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/cyclevia", methods=["POST"])
def save_cyclevia_config():
    cfg = request.get_json(force=True)
    cyclevia_core.save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/config/plc", methods=["POST"])
def save_plc_config():
    cfg = request.get_json(force=True)
    plc_core.get_monitor().set_config(cfg)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# BILAN KM
# ---------------------------------------------------------------------------
@app.route("/api/bilan/run", methods=["POST"])
def bilan_run():
    body = request.get_json(force=True) or {}
    years_txt = body.get("years", str(datetime.now().year))
    cfg = bilan_core.load_config()

    job_id = uuid.uuid4().hex
    q = queue.Queue()
    _jobs[job_id] = {"queue": q, "rows": None, "summary": None,
                     "status": "running", "error": None}

    def worker():
        def log(msg):
            q.put(("log", msg))

        def prog(done, total):
            q.put(("progress", {"done": done, "total": total}))

        try:
            target_years = bilan_core.parse_years(years_txt)
            if not target_years:
                raise ValueError("Aucune année valide")
            log(f"Années : {sorted(target_years)}")
            log("Connexion Trackdéchets...")
            forms = bilan_core.fetch_all_forms(
                cfg["td_api_token"], cfg["protec_siret"], logger=log)
            log(f"   {len(forms)} BSDD récupérés au total")
            geo = bilan_core.GeoService(cfg.get("ors_api_key", ""), logger=log)
            log("Calcul des distances...")
            rows, summary = bilan_core.compute_bilan(
                forms, cfg, geo, target_years, logger=log, progress=prog)

            _jobs[job_id]["rows"] = rows
            _jobs[job_id]["summary"] = summary
            _jobs[job_id]["status"] = "done"

            summary_ser = {
                f"{yr}|{tp}|{cat}|{code}": km
                for (yr, tp, cat, code), km in summary.items()
            }
            q.put(("summary", summary_ser))
            q.put(("done", {"total_rows": len(rows)}))
        except Exception as e:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
            q.put(("error", str(e)))

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/bilan/progress/<job_id>")
def bilan_progress(job_id):
    job = _jobs.get(job_id)
    if not job:
        return Response("Job introuvable", status=404)

    @stream_with_context
    def generate():
        q = job["queue"]
        while True:
            try:
                kind, data = q.get(timeout=30)
                yield _sse(kind, data)
                if kind in ("done", "error"):
                    break
            except queue.Empty:
                yield _sse("ping", "")

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/bilan/results/<job_id>")
def bilan_results(job_id):
    job = _jobs.get(job_id)
    if not job or job["rows"] is None:
        return jsonify({"error": "Job introuvable ou en cours"}), 404
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 100))
    rows = job["rows"]
    total = len(rows)
    start = (page - 1) * per_page
    return jsonify({
        "rows": rows[start:start + per_page],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })


@app.route("/api/bilan/export/<job_id>")
def bilan_export(job_id):
    job = _jobs.get(job_id)
    if not job or job["rows"] is None:
        return jsonify({"error": "Job introuvable ou en cours"}), 404
    buf = io.BytesIO()
    cfg = bilan_core.load_config()
    bilan_core.export_excel(job["rows"], job["summary"], buf,
                            round_trip=cfg.get("round_trip", True))
    buf.seek(0)
    fname = f"bilan_km_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------------------------------------------------------------------
# PLC MONITOR
# ---------------------------------------------------------------------------
@app.route("/api/plc/status")
def plc_status():
    return jsonify(plc_core.get_monitor().status())


@app.route("/api/plc/connect", methods=["POST"])
def plc_connect():
    body = request.get_json(force=True) or {}
    ip = body.get("ip")
    ok, msg = plc_core.get_monitor().connect(ip)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/plc/disconnect", methods=["POST"])
def plc_disconnect():
    plc_core.get_monitor().disconnect()
    return jsonify({"ok": True})


@app.route("/api/plc/start", methods=["POST"])
def plc_start():
    body = request.get_json(force=True) or {}
    interval = body.get("interval", 5)
    plc_core.get_monitor().start_loop(interval=int(interval))
    return jsonify({"ok": True})


@app.route("/api/plc/stop", methods=["POST"])
def plc_stop():
    plc_core.get_monitor().stop_loop()
    return jsonify({"ok": True})


@app.route("/api/plc/read", methods=["POST"])
def plc_read_once():
    reading = plc_core.get_monitor().read_once()
    if reading is None:
        return jsonify({"error": "Lecture échouée (PLC déconnecté ?)"}), 503
    return jsonify(reading)


@app.route("/api/plc/stream")
def plc_stream():
    monitor = plc_core.get_monitor()
    sub_q = monitor.subscribe()

    @stream_with_context
    def generate():
        try:
            history = monitor.get_history(n=1)
            if history:
                yield _sse("reading", history[-1])
            while True:
                try:
                    reading = sub_q.get(timeout=30)
                    yield _sse("reading", reading)
                except queue.Empty:
                    yield _sse("ping", "")
        finally:
            monitor.unsubscribe(sub_q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/plc/history")
def plc_history():
    n = int(request.args.get("n", 100))
    return jsonify(plc_core.get_monitor().get_history(n=n))


# ---------------------------------------------------------------------------
# CYCLEVIA
# ---------------------------------------------------------------------------
@app.route("/api/cyclevia/run", methods=["POST"])
def cyclevia_run():
    body = request.get_json(force=True) or {}
    now = datetime.now()
    annee = int(body.get("annee", now.year))
    mois = int(body.get("mois", now.month))
    cfg = cyclevia_core.load_config()

    job_id = uuid.uuid4().hex
    q = queue.Queue()
    _jobs[job_id] = {"queue": q, "csv_bytes": None, "filename": None,
                     "nb_lignes": 0, "total_tonnes": 0.0,
                     "status": "running", "error": None}

    def worker():
        def log(msg):
            q.put(("log", msg))
        try:
            csv_bytes, nb, total, filename = cyclevia_core.run_collecte(
                cfg, annee, mois, logger=log)
            _jobs[job_id]["csv_bytes"] = csv_bytes
            _jobs[job_id]["filename"] = filename
            _jobs[job_id]["nb_lignes"] = nb
            _jobs[job_id]["total_tonnes"] = total
            _jobs[job_id]["status"] = "done"
            q.put(("done", {
                "nb_lignes": nb,
                "total_tonnes": total,
                "filename": filename,
                "has_data": csv_bytes is not None,
            }))
        except Exception as e:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(e)
            q.put(("error", str(e)))

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/cyclevia/progress/<job_id>")
def cyclevia_progress(job_id):
    job = _jobs.get(job_id)
    if not job:
        return Response("Job introuvable", status=404)

    @stream_with_context
    def generate():
        q = job["queue"]
        while True:
            try:
                kind, data = q.get(timeout=30)
                yield _sse(kind, data)
                if kind in ("done", "error"):
                    break
            except queue.Empty:
                yield _sse("ping", "")

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/cyclevia/download/<job_id>")
def cyclevia_download(job_id):
    job = _jobs.get(job_id)
    if not job or job.get("csv_bytes") is None:
        return jsonify({"error": "Fichier non disponible"}), 404
    buf = io.BytesIO(job["csv_bytes"])
    fname = job.get("filename") or "collecte_cyclevia.csv"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="text/csv")


# ---------------------------------------------------------------------------
# Lancement
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import webbrowser
    print("=" * 55)
    print("  PROTEC Suite - Démarrage")
    print("=" * 55)
    print("  Ouverture du navigateur sur http://localhost:5000")
    print("  Arrêt : Ctrl+C")
    print("=" * 55)
    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5000")).start()
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
