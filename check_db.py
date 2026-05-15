import sqlite3
db = 'D:/Projects/CODING/aram-winrate-nn/data/lcu/games.db'
con = sqlite3.connect(db)
total = con.execute('SELECT COUNT(*) FROM games').fetchone()[0]
mayhem = con.execute('SELECT COUNT(*) FROM games WHERE queue_id=2400').fetchone()[0]
pending = con.execute("SELECT COUNT(*) FROM crawl_queue WHERE status='pending'").fetchone()[0]
print(f'total={total}  mayhem={mayhem}  pending_frontier={pending}')
con.close()
