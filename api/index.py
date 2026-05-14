# Vercel entry point for Flask app
import os
import sys

# Ensure the project root is in path
sys.path.insert(0, os.path.dirname(__file__))

# Import the Flask app
from app import app

# Vercel requires ASGI or WSGI via their adapter
# For @vercel/python builder, export the WSGI app
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
