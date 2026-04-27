import sqlite3
conn = sqlite3.connect(r'D:\桌面文件\ytl\QClawProject\YTL仓博系统\instance\blog.db')
cur = conn.cursor()
cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'")
print(cur.fetchone()[0])
