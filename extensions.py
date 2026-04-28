"""
Shared Flask extensions — initialized here, bound to app in app.py via init_app().
"""
import os
import secrets
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Generated once per process start. Any JWT that doesn't carry this exact value
# was issued before the current server process and is treated as expired.
BOOT_TOKEN = secrets.token_hex(16)

_storage = os.environ.get("REDIS_URL", "memory://")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=_storage,
)
