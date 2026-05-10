"""Quick diagnostic: show CW slot status from DB."""
import job_store

job_store.init_db()
conn = job_store.connect()
rows = job_store.rows_to_dicts(
    conn.execute(
        "SELECT slot_id, status, notes, adspower_profile_id FROM adsp_profile_templates ORDER BY slot_id"
    ).fetchall()
)
for r in rows:
    notes = (r["notes"] or "")[:150]
    print(f"  {r['slot_id']}: status={r['status']}, profile_id={r['adspower_profile_id']}, notes={notes}")
conn.close()
