#!/usr/bin/env python
"""
One-time migration script: seeds the Prompt table from data/system_prompts.json
(or DEFAULT_PROMPTS if the JSON file is missing).

Usage:
    python scripts/seed_prompts.py
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app
from model import db, Prompt
from utils.prompt_manager import DEFAULT_PROMPTS

PROMPTS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'system_prompts.json'
)


def seed():
    with app.app_context():
        existing = Prompt.query.count()
        if existing > 0:
            print(f"Prompt table already has {existing} rows — skipping seed.")
            return

        prompts_data = DEFAULT_PROMPTS
        if os.path.exists(PROMPTS_FILE):
            try:
                with open(PROMPTS_FILE, 'r', encoding='utf-8') as f:
                    prompts_data = json.load(f)
                print(f"Loaded {len(prompts_data)} prompts from {PROMPTS_FILE}")
            except Exception as e:
                print(f"Could not read JSON file, using defaults: {e}")

        for p in prompts_data:
            db.session.add(Prompt(
                name=p['id'],
                display_name=p.get('name', p['id']),
                description=p.get('description', ''),
                category=p.get('category', 'General'),
                prompt_text=p['prompt'],
            ))

        db.session.commit()
        print(f"Seeded {len(prompts_data)} prompts into the database.")


if __name__ == '__main__':
    seed()
