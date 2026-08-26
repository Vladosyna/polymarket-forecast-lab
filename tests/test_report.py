

def test_report_withholds_precorrection_kalshi_results(tmp_path):
    """PAP 9.25: Kalshi eval_runs rows written before 2026-08-26 were computed
    over an inflated n -- that venue had no event clustering, so each leg of one
    question counted as an independent observation. MAX(ts) retires most of them
    on the next nightly run; this filter covers the case that it would not, a
    combination that stops being computed and leaves its last stale row as the
    newest forever."""
    from lab.eval.report import latest_eval_rows
    from lab.store import db

    conn = db.connect(tmp_path / "lab.db")
    rows = [
        ("m1_debiased", "all_time", "kalshi", "economics", "2026-08-20T02:00:00+00:00", 1649),
        ("m1_debiased", "all_time", "polymarket", "economics", "2026-08-20T02:00:00+00:00", 1058),
        ("m0_market", "all_time", "kalshi", "economics", "2026-08-27T02:00:00+00:00", 243),
    ]
    for model, window, venue, cat, ts, n in rows:
        conn.execute(
            "INSERT INTO eval_runs (ts, model_id, window_label, venue, category, n, "
            "n_event_clusters) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, model, window, venue, cat, n, n),
        )
    conn.commit()

    got = {(r["model_id"], r["venue"]) for r in latest_eval_rows(conn)}
    assert ("m1_debiased", "kalshi") not in got        # stale, withheld
    assert ("m1_debiased", "polymarket") in got        # same age, unaffected venue
    assert ("m0_market", "kalshi") in got              # recomputed, shown
    conn.close()
