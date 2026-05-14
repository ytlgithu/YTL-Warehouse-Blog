# Vercel entry point for Flask app
import os
import sys
import traceback

# Ensure the project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import the Flask app
from app import app, init_db

# Initialize database on startup (for Vercel serverless)
try:
    with app.app_context():
        init_db()
except Exception as e:
    print(f"[INIT ERROR] {type(e).__name__}: {e}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    # 不要崩溃，让应用继续运行（会在请求时报错但能看到错误）

# Vercel requires ASGI or WSGI via their adapter
# For @vercel/python builder, export the WSGI app
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
