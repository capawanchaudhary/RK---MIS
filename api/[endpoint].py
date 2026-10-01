import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import Handler, init_db

_init_lock = threading.Lock()
_initialized = False


class handler(Handler):
    def __init__(self, request, client_address, server):
        global _initialized
        if os.environ.get('DATABASE_URL') and not _initialized:
            with _init_lock:
                if not _initialized:
                    init_db()
                    _initialized = True
        super().__init__(request, client_address, server)
