# Implementation Plan — Phase 1.1: Stability & Security
**Project: J. Kalachand Product Information System (PIS)**
**Date: April 2026**

## 1. Project Context & Structure

The **J. Kalachand PIS** is a multi-role workflow application designed to automate the creation of product information and specification sheets for a large retailer. It uses a state-machine approach to move products from Marketing through Director approval to the Web Team.

### Current Folder Structure
- `app.py`: The core monolithic Flask application (3,000+ lines).
- `model.py`: SQLAlchemy database models (PostgreSQL).
- `utils/`: Modular helper functions (AI generation, PDF processing, etc.).
- `templates/`: Jinja2 HTML templates using Tailwind CSS and Alpine.js.
- `data/`: Temporary storage for system prompts and job queues in flat JSON files.
- `static/`: Static assets and local product image uploads.

---

## 2. Task 1: Mobile UI Fix (Hamburger Menu)

### Problem
The sidebar is currently hidden on mobile devices using Tailwind's `hidden md:flex` classes, and the hamburger menu button has no logic to toggle it.

### Proposed Changes
- **Target File:** [base.html](file:///c:/Users/yukta/PIS-JKalachand/templates/base.html)
- **Mechanism:** Use **Alpine.js** to manage a local state variable `mobileMenuOpen`.
- **Steps:**
    1. Wrap the `aside` and `main` elements or the `body` in a div with `x-data="{ mobileMenuOpen: false }"`.
    2. Update the Mobile Top Bar button to `@click="mobileMenuOpen = true"`.
    3. Modify the `aside` classes to conditionally show itself when `mobileMenuOpen` is true (using `x-show` or class binding).
    4. Add a backdrop overlay that appears when the menu is open and closes it on click.

---

## 3. Task 2: Security Quick-Wins

### 3.1 CSRF Protection
- **Target File:** [app.py](file:///c:/Users/yukta/PIS-JKalachand/app.py) & all [templates/](file:///c:/Users/yukta/PIS-JKalachand/templates/)
- **Mechanism:** Use `Flask-WTF`'s `CSRFProtect` extension.
- **Steps:**
    1. Update `requirements.txt` to include `flask-wtf`.
    2. Initialize `CSRFProtect(app)` in `app.py`.
    3. Globally inject the CSRF token into all `<form method="POST">` tags across all templates using `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`.

### 3.2 Session Expiry
- **Target File:** [app.py](file:///c:/Users/yukta/PIS-JKalachand/app.py)
- **Steps:**
    1. Add `app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)` to the config section.
    2. In the `/login` route, after a successful login, set `session.permanent = True`.

---

## 4. Task 3: Infrastructure Stabilization (DB Migration)

### 3.1 System Prompts Migration
- **Target Files:** [model.py](file:///c:/Users/yukta/PIS-JKalachand/model.py), [utils/prompt_manager.py](file:///c:/Users/yukta/PIS-JKalachand/utils/prompt_manager.py)
- **Steps:**
    1. **Model:** Create a `Prompt` model with fields: `id` (PK), `name`, `category`, `prompt_text`, and `updated_at`.
    2. **Manager:** Refactor `prompt_manager.py` to query the `Prompt` table instead of reading `data/system_prompts.json`.
    3. **Migration:** Create a one-time script to seed the database with the contents of the current JSON file.

### 3.2 Job Queue Migration
- **Target Files:** [model.py](file:///c:/Users/yukta/PIS-JKalachand/model.py), [app.py](file:///c:/Users/yukta/PIS-JKalachand/app.py)
- **Steps:**
    1. **Model:** Create a `Job` model with fields: `id` (UUID), `model_name`, `status` (pending/processing/completed/failed), `message`, `payload` (JSONB), and `result` (JSONB).
    2. **App Logic:** Replace the `_load_jobs` and `_save_jobs` functions in `app.py` with SQLAlchemy queries.
    3. **Cleanup:** Remove the file-based locking logic (`_pis_file_lock`) as PostgreSQL handles concurrency natively.

---

## 5. Task 4: Start the Test Suite

### Steps
1. Create a `tests/` directory.
2. **Configuration:** Create `tests/conftest.py` to define a `pytest` fixture that creates a test app instance with a temporary database.
3. **Auth Test:** Create `tests/test_auth.py` to test:
    - Successful login redirects to correct dashboard.
    - Failed login shows flash error.
    - Logout clears session.
4. **Utility Test:** Create `tests/test_utils.py` to test core logic in `utils/json_utils.py` or `utils/history.py`.

---

## 6. Summary of Requirements Changes
- **New Libraries:** `flask-wtf`, `pytest`, `pytest-flask`.
- **Database Changes:** 2 new tables (`prompt`, `job`).
- **File Deletions (Post-Migration):** `data/system_prompts.json`, `data/pis_jobs.json`.
