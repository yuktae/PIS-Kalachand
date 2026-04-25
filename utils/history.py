"""
Event logging utilities for PIS System
Handles product history and event logging
"""

from datetime import datetime
from model import db, ProductHistory


def log_event(product_id, actor, title, description, action_type='neutral'):
    """
    Logs an event to the ProductHistory table.
    action_type options: 'neutral' (gray), 'waiting' (blue), 'action' (red), 'success' (green)
    """
    try:
        event = ProductHistory(
            product_id=product_id,
            actor=actor,
            action_title=title,
            description=description,
            action_type=action_type,
            timestamp=datetime.now()
        )
        db.session.add(event)
        db.session.commit()
    except Exception as e:
        print(f"Failed to log history: {e}")
