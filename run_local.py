#!/usr/bin/env python3
"""
Local development runner script.
This script runs the Flask app directly (not via Gunicorn) for local development.
"""
import os
import sys

# Set environment variables for local development
os.environ["RUNNING_LOCALLY"] = "1"
os.environ["PRIMARY_WORKER"] = "1"
os.environ["PORT"] = os.environ.get("PORT", "8090")
os.environ["DEBUG"] = os.environ.get("DEBUG", "True")

# Import and run the app
if __name__ == "__main__":
    # Import app after setting environment variables
    from app import app, socketio, initialize_server_count, _load_admin_logs_from_file, _flush_admin_logs_to_file
    import atexit
    import signal
    import sys
    import logging
    
    # Initialize server count
    initialize_server_count()
    
    # Load admin logs from file on startup
    _load_admin_logs_from_file()
    
    # Flush any pending admin logs on startup
    _flush_admin_logs_to_file()
    
    # Register cleanup function
    def cleanup():
        from app import save_server_count, _flush_admin_logs_to_file
        save_server_count()
        _flush_admin_logs_to_file()
    
    atexit.register(cleanup)
    signal.signal(signal.SIGTERM, lambda s, f: (cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda s, f: (cleanup(), sys.exit(0)))
    
    # Run the app
    port = int(os.environ.get("PORT", 8090))
    host = os.environ.get("HOST", "127.0.0.1")  # Use localhost for local dev
    debug = os.environ.get("DEBUG", "True").lower() == "true"
    
    from app import PERSISTENT_TEMP_ROOT, COUNT_FILE
    logging.info(f"🚀 Starting local development server on http://{host}:{port}")
    logging.info(f"📁 Using storage: {PERSISTENT_TEMP_ROOT}")
    logging.info(f"💾 Count file: {COUNT_FILE}")
    logging.info("💡 Press Ctrl+C to stop")
    
    socketio.run(app, debug=debug, port=port, host=host, allow_unsafe_werkzeug=True)

