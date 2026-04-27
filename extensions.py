"""
Shared Flask extensions — initialized here, bound to app in app.py via init_app().
"""
import os
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

_storage = os.environ.get("REDIS_URL", "memory://")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri=_storage,
)
