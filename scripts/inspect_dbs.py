import sqlite3
import json
import os
import time

def inspect_db(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    
    claims = [dict(r) for r in cur.execute("SELECT * FROM claims ORDER BY made_at ASC").fetchall()]
    agents = [dict(r) for r in cur.execute("SELECT * FROM agents").fetchall()]
    tasks = [dict(r) for r in cur.execute("SELECT * FROM tasks").fetchall()]
    processes = [dict(r) for r in cur.execute("SELECT * FROM processes").fetchall()]
    events = [dict(r) for r in cur.execute("SELECT * FROM events ORDER BY id ASC").fetchall()]
    lessons = [dict(r) for r in cur.execute("SELECT * FROM lessons").fetchall()]
    memories = [dict(r) for r in cur.execute("SELECT * FROM memories").fetchall()]
    
    # Calculate track records
    track_records = {}
    for a in agents:
        aid = a["id"]
        w = cur.execute("SELECT COUNT(*) FROM claims WHERE agent=? AND label='witnessed' AND anchored=1", (aid,)).fetchone()[0]
        r = cur.execute("SELECT COUNT(*) FROM claims WHERE agent=? AND label='refuted'", (aid,)).fetchone()[0]
        p = cur.execute("SELECT COUNT(*) FROM claims WHERE agent=? AND label='declared'", (aid,)).fetchone()[0]
        u = cur.execute("SELECT COUNT(*) FROM claims WHERE agent=? AND label='witnessed' AND anchored=0", (aid,)).fetchone()[0]
        exp = cur.execute("SELECT COUNT(*) FROM events WHERE kind='lease_expired' AND agent=?", (aid,)).fetchone()[0]
        rel = cur.execute("SELECT COUNT(*) FROM events WHERE kind='release_task' AND agent=?", (aid,)).fetchone()[0]
        track_records[aid] = {
            "witnessed": w,
            "refuted": r,
            "pending": p,
            "unanchored": u,
            "leases_expired": exp,
            "released": rel
        }
    
    return {
        "db_path": os.path.basename(db_path),
        "agents": agents,
        "track_records": track_records,
        "claims": claims,
        "processes": processes,
        "tasks": tasks,
        "events": events,
        "lessons": lessons,
        "memories": memories
    }

if __name__ == "__main__":
    db1 = r"C:\Projectsfolder\OhmOS\Axiom1\.axiom_arms_paid_run3\arm_c.db"
    res1 = inspect_db(db1)
    print("=== arm_c.db ===")
    print("Agents:", list(res1["track_records"].keys()))
    print("Track records:", res1["track_records"])
    print(f"Claims ({len(res1['claims'])}):")
    for c in res1["claims"]:
        print(f"  [{c['label']}] by {c['agent']} ({c['id']}) - {c['check_id']}")
        print(f"    Statement: {c['statement'][:70]}")
        print(f"    Reason: {c['reason'][:80]}")
    print(f"Processes ({len(res1['processes'])}):", res1["processes"])

    db2 = r"C:\Projectsfolder\OhmOS\Axiom1\.axiom_arms_paid_run\arm_a.db"
    res2 = inspect_db(db2)
    print("\n=== arm_a.db ===")
    print("Track records:", res2["track_records"])
    print(f"Claims ({len(res2['claims'])}):")
    for c in res2["claims"]:
        print(f"  [{c['label']}] by {c['agent']} ({c['id']}) - {c['check_id']}")
