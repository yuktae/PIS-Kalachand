# PIS System — Code Review & Architecture Analysis
**J. Kalachand Product Information System**
*Author: Assigned Developer | Date: April 2026*

---

## 1. Purpose of This System

The PIS (Product Information System) is an internal multi-role web application for **J. Kalachand & Co. Ltd, Mauritius**, a major retail business. Its purpose is to manage the entire lifecycle of product content from raw supplier documents (proforma invoices, spec sheets) through to publication-ready product listings for the Magento e-commerce store.

The workflow has 4 main roles:

| Role | Responsibility |
|---|---|
| **Marketing** | Creates and submits PIS from proforma PDFs/URLs |
| **Director** | Reviews and approves/requests-changes on PIS |
| **Web Team** | Generates spec sheets and SEO content for web |
| **Admin** | Manages users, prompts, and system configuration |

Each product passes through a strict state machine: `marketing_draft → pending_director_pis → ready_for_web → specsheet_draft → pending_director_spec → finalized`.

---

## 2. What Works Well (Keep As-Is or Improve Incrementally)

### ✅ Backend Logic in `app.py` / `model.py`
- The Flask factory-style `app.py` is well-structured for a small team product.
- The **multi-stage workflow state machine** is clearly defined in `model.py` and enforced at the route level.
- The `Product` model correctly uses **PostgreSQL `JSONB`** for flexible product data with GIN indexes — this is the right call for semi-structured product content.
- The **advisory lock pattern** (`pg_advisory_lock`) for the audit trigger install is a sophisticated and correct solution for multi-worker race conditions under Gunicorn.

### ✅ Utils Package Structure
- The code was well refactored into `utils/`: `ai_generation`, `web_scraping`, `pdf_processing`, `history`, `prompt_manager`, `category_classifier`, `image_processing` — each module is focused and cohesive.

### ✅ Tiered Web Scraping Pipeline
- `scrape_url_data()` uses a smart fallback chain: Firecrawl → Jina Reader → BeautifulSoup. This is resilient and cost-conscious.

### ✅ Proforma PDF Processing
- The two-pass PDF image extraction (screenshot + embedded fallback) using PyMuPDF + Gemini Vision is technically impressive and correct for the Kalachand use case where proforma invoices are complex tables.

### ✅ Prompt Manager
- Prompts stored in `data/system_prompts.json` with admin-editable UI is the right design. It allows non-developers to tune AI behaviour without a code deploy.

---

## 3. Architecture Questions — Detailed Analysis

### 3.1 Backend Framework — Is Flask the Right Choice?

**Verdict: Flask is acceptable but shows strain at `app.py` scale.**

`app.py` is currently **3,356 lines** — a single monolithic file handling every route, every piece of business logic, the job queue, all seed data, and startup sequences. This is the primary technical debt problem.

**What to do:**
- Keep Flask as the framework (it is appropriate for an internal tool of this scale).
- **Refactor `app.py` into Blueprints** — one blueprint per role/feature area:
  ```
  app/
  ├── blueprints/
  │   ├── auth/          (login, logout)
  │   ├── marketing/     (dashboard, create, bulk, history)
  │   ├── director/      (dashboard, approve, archive)
  │   ├── web/           (dashboard, specsheets, forbidden words)
  │   ├── admin/         (users, prompts)
  │   └── api/           (JSON endpoints for AJAX calls)
  ├── models/            (split model.py by domain)
  └── utils/             (keep as-is)
  ```
- Use `create_app()` factory pattern (which is already partially started) with proper Blueprint registration.

---

### 3.2 Frontend — Alpine.js vs React

**Verdict: Alpine.js is fine for this application. React is overkill.**

**Reasoning:**
- This is a **server-rendered internal tool**, not a consumer SPA. The UI complexity is form-heavy with some dynamic interactions — Alpine.js with Jinja2 is exactly right for this.
- React would require a build step, a separate dev server, an API-first backend, and significantly more complexity for marginal gain in this context.
- The current TailwindCSS CDN approach (no build step) is fast to iterate — for production on Azure, a PostCSS build with PurgeCSS to reduce CSS bundle size would be appropriate but is not urgently needed.

**What should change in the frontend:**
- **Ergonomics and UX** (see section 5.1 below).
- The mobile hamburger menu in `base.html` is rendered but has **no open/close logic** — the sidebar is completely invisible on mobile. This needs to be wired up.
- Several very large template files (`verify_marketing.html` at 72KB, `edit_specsheet.html` at 119KB) should be broken into Jinja2 partials using `{% include %}` to improve maintainability, similar to what the `templates/partials/` directory hints at.

---

### 3.3 Job Queue — Flat JSON vs Redis

**Verdict: The flat JSON approach is a critical infrastructure risk.**

**Current state:**
- Async PIS generation jobs are stored in `data/pis_jobs.json`.
- A `threading.Lock` provides intra-process safety, but note that under Gunicorn with 2+ workers, each worker has its own lock — the lock provides **zero cross-worker safety**.
- Jobs use `os.replace()` (atomic rename) which mitigates some race conditions but is still fragile under concurrent writes from multiple Gunicorn workers.

**What can go wrong:**
1. Worker A and Worker B both read `pis_jobs.json`, each modify it, and the last write wins — jobs can be silently lost.
2. The `ThreadPoolExecutor(max_workers=5)` is per-worker, meaning with 2 Gunicorn workers you actually have up to 10 parallel Gemini API calls with no global cap.

**Recommended solution for Azure:**
```
Azure Service Bus (or Redis via Azure Cache for Redis)
```

- **Azure Cache for Redis** is available as a managed service and integrates directly. Use `Celery` with a Redis broker, or use `rq` (simpler) with the same Redis backend.
- At minimum in the near-term: **move jobs to PostgreSQL**. A `Job` table with `status`, `payload`, `result` columns handles concurrency correctly (PostgreSQL transactions are ACID). This requires zero new infrastructure since PostgreSQL is already deployed.

**Migration path:** `pis_jobs.json → Job table in PostgreSQL → Redis/Celery when scale demands it.`

---

### 3.4 Playwright on Azure — How to Deploy

**Current state:**
- The `Dockerfile` correctly uses `mcr.microsoft.com/playwright/python:v1.40.0-jammy` as the base image, which **pre-installs all Chromium system libraries**.
- `playwright install chromium` is run during the Docker build.

**This is already the right approach for Azure.** However, there are some Azure-specific considerations:

1. **Azure Container Apps / Azure App Service (Docker):**
   - Deploy the app as a Docker container — the Playwright base image handles all dependencies.
   - Set `--no-sandbox` for Chromium (required in containerised Linux environments):
     ```python
     # In pdf_processing.py or wherever playwright is invoked
     browser = playwright.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
     ```
   - `/dev/shm` (shared memory) is often limited in containers. `--disable-dev-shm-usage` tells Chromium to use `/tmp` instead.

2. **Azure Kubernetes Service (AKS):** Same Docker approach. Ensure the container has at least 512MB memory for Chromium.

3. **Do NOT use Azure App Service (code deployment, not Docker):** The underlying OS won't have Chromium's native library dependencies and there is no way to install them without Docker.

**Recommended Azure architecture:**
```
Azure Container Apps → deploys the Docker image directly
Azure Database for PostgreSQL (Flexible Server)
Azure Blob Storage → for static files (see 3.6)
Azure Cache for Redis → for the job queue
```

---

### 3.5 Prompt Storage — Are Prompts Saved in a Good Way?

**Verdict: Acceptable for now but must move to the database.**

**Current state:**
- `data/system_prompts.json` is a flat file with all prompts.
- The admin UI can edit and reset prompts.
- Prompts are hardcoded as `DEFAULT_PROMPTS` in `prompt_manager.py` as fallback.

**Problems in a cloud/Azure deployment:**
1. `data/system_prompts.json` is **on the container's local filesystem**. When the container is restarted or redeployed, any admin edits to prompts are **lost unless a volume is mounted**.
2. Multiple Gunicorn workers reading/writing the same file simultaneously can corrupt it.
3. There is no history of prompt changes — if an admin breaks a prompt, rollback requires knowing the previous content.

**Recommended fix:**
- Move prompts to a `Prompt` table in PostgreSQL:
  ```python
  class Prompt(db.Model):
      id = db.Column(db.String, primary_key=True)  # e.g. 'pis_extraction'
      name = db.Column(db.String(100))
      category = db.Column(db.String(50))
      prompt_text = db.Column(db.Text)
      updated_at = db.Column(db.DateTime)
      updated_by_id = db.Column(db.ForeignKey('user.id'))
  ```
- This makes prompts cloud-safe, auditable, and consistent across Gunicorn workers.

---

### 3.6 Static Files — Local Storage vs Cloud Storage

**Verdict: Local file storage is incompatible with Azure multi-instance or container deployments.**

**Current state:**
- Product images are saved to `static/uploads/` on the local container filesystem.
- `image_path` in the `Product` model stores paths like `uploads/filename.jpg`.

**Problems:**
1. **When a container restarts, all uploaded images are lost** unless a Persistent Volume is mounted.
2. **When two container instances run** (horizontal scaling), uploads on Instance A are invisible to Instance B.
3. On Azure Container Apps, persistent volumes are available but add complexity.

**Recommended solution — Azure Blob Storage:**
- Use `azure-storage-blob` Python SDK.
- Upload product images to an Azure Blob Storage container (`pis-uploads`).
- Store the **blob URL** in `image_path` instead of a local path.
- This gives you CDN delivery, redundancy, and works across any number of container instances.

```python
from azure.storage.blob import BlobServiceClient

def upload_image_to_blob(local_path, blob_name):
    client = BlobServiceClient.from_connection_string(os.getenv('AZURE_STORAGE_CONNECTION_STRING'))
    container = client.get_container_client('pis-uploads')
    with open(local_path, 'rb') as data:
        container.upload_blob(blob_name, data, overwrite=True)
    return f"https://{account}.blob.core.windows.net/pis-uploads/{blob_name}"
```

For **local development**, you can use **Azurite** (Azure Storage emulator) or keep the current `static/uploads/` approach behind an environment flag.

---

### 3.7 Database Schema — Is PostgreSQL Correct for Kalachand?

**Verdict: PostgreSQL is the right choice. The schema is mostly correct but needs refinement.**

**Why PostgreSQL is correct:**
- Kalachand is a large multi-category retailer. Products have highly variable attributes (spec keys differ per category). Using `JSONB` for `pis_data` and `spec_data` is the right pattern for this variability — you get schema flexibility while retaining full SQL query capability on the JSON fields.
- The GIN indexes on `pis_data` and `spec_data` are correctly placed for the containment queries used in the audit trail.
- The audit trigger in PostgreSQL is an elegant solution for field-level change tracking.

**Issues with the current schema:**

| Issue | Impact | Fix |
|---|---|---|
| `ProductVersion` stores full JSONB snapshots for every save | Storage bloat at scale (e.g., 1000 products × 10 versions × 50KB = 500MB) | Add a version retention policy and/or switch to JSON diff storage |
| `FieldChangeLog.old_value` and `new_value` are `Text` (unindexed) | Slow for auditing queries | Add an index on `(product_id, timestamp DESC)` |
| `User.role` is a plain string with no constraint | Risk of typos/invalid roles | Use a PostgreSQL ENUM type or a check constraint |
| No `deleted_at` soft-delete column on `Product` | Deleting a product cascades and destroys all history | Add `deleted_at` timestamp for soft-delete |
| `pis_jobs.json` is not in the DB | See section 3.3 | Add `Job` table |
| No `tenant_id` on any table | Not multi-tenant | Fine for now, note it if Kalachand wants to extend to subsidiaries |

---

### 3.8 AI Model — Is Gemini the Best Choice?

**Verdict: Gemini Flash is a good choice for this workload. Consider keeping it but add model abstraction.**

**Why Gemini Flash is appropriate:**
- The workload is primarily **structured JSON extraction from PDFs and web content** — multimodal tasks where Gemini Flash excels due to its large context window (1M tokens) and native support for file uploads.
- PDF image analysis (`gemini-flash-latest` with image inputs) works correctly via the Google Generative AI SDK.
- Cost: Gemini Flash is significantly cheaper than GPT-4o for high-volume batch operations (bulk product processing).

**Risks:**
- All AI calls use the hardcoded string `'models/gemini-flash-latest'`. When Google rotates model names (which they do), this will silently use an outdated model or break.
- There is **no retry/backoff** logic beyond a basic attempt loop in `_extract_via_screenshot`.

**Recommendations:**
1. **Extract model name to config** (`GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'models/gemini-2.0-flash')`).
2. Wrap all Gemini calls in a utility function with exponential backoff, using `google.api_core.retry`.
3. For the AI revision feature, consider testing **Gemini 1.5 Pro** for complex director feedback — Flash sometimes produces inconsistent formatting on nuanced content revision requests.

---

## 4. Testing — What Needs to Be Built

**Current state: Zero test coverage.** No unit tests, no integration tests, no end-to-end tests exist in the codebase.

This is the most critical gap for production deployment and Azure CI/CD.

### 4.1 Recommended Testing Stack

| Layer | Tool | What to Test |
|---|---|---|
| Unit tests | `pytest` + `pytest-flask` | Model methods, utility functions, prompt manager |
| Integration tests | `pytest` + Flask test client | All API routes, workflow state transitions |
| AI mock tests | `unittest.mock` | Mock Gemini responses to test JSON parsing logic |
| E2E browser tests | `playwright` (Python) | Full user journeys per role |
| Load tests | `locust` | Bulk upload and AI generation endpoints |

### 4.2 Priority Test Cases

**Critical (build first):**
1. `test_login_flow` — valid/invalid credentials, role redirects
2. `test_workflow_transitions` — each state transition with correct/incorrect roles
3. `test_pis_json_parsing` — `safe_json_loads` with malformed/partial AI responses
4. `test_forbidden_words_scrub` — verify words are removed from all nested fields
5. `test_version_snapshot` — create version, restore version, verify data integrity
6. `test_audit_trigger` — that field changes are recorded in `field_change_log`

**Important:**
7. `test_bulk_upload` — 1 PDF with 3 products → 3 PIS records created
8. `test_category_classification` — known product → expected category output
9. `test_pdf_extraction` — mock Gemini, test image crop and save logic

**Recommended `tests/` structure:**
```
tests/
├── conftest.py          (SQLite test DB, app factory, auth fixtures)
├── unit/
│   ├── test_models.py
│   ├── test_ai_generation.py
│   ├── test_prompt_manager.py
│   └── test_web_scraping.py
├── integration/
│   ├── test_auth_routes.py
│   ├── test_marketing_routes.py
│   ├── test_director_routes.py
│   └── test_admin_routes.py
└── e2e/
    └── test_full_workflow.py
```

---

## 5. New Features — Analysis and Proposals

### 5.1 UI/UX Ergonomics and Efficiency

**Current state:**
- The design uses Tailwind CDN and Alpine.js — aesthetically clean but with usability gaps.
- Mobile sidebar: rendered but not functional (hamburger menu not wired).
- Large template files make the UI hard to iterate.

**Proposed improvements:**

1. **Keyboard shortcuts** — Marketing team processes many products. Add keyboard shortcuts for common actions (e.g., `Ctrl+Enter` to submit, `Ctrl+S` to save draft).
2. **Sticky action bars** — On long product edit pages, the Save/Submit buttons should be sticky so the user doesn't scroll to find them.
3. **Inline field validation** — Currently there is no client-side validation before API calls. Add Alpine.js validators for required fields.
4. **Dashboard KPIs** — Replace raw counts with trend indicators (e.g., "↑ 3 products this week") to help the Director understand throughput.
5. **Bulk action UI** — Marketing should be able to select multiple products and bulk-submit or bulk-archive.
6. **Better loading states** — The current spinner indicator (`bgProcessIndicator`) is good but needs a per-field loading state for inline editing sections.
7. **Fix mobile sidebar** — Wire the hamburger button to an Alpine.js `open` state that toggles the sidebar visibility.

---

### 5.2 Change Tracking, Comments, and Version Restore

**Current state:**
- `ProductVersion` stores full JSONB snapshots — version restore is supported in principle.
- `FieldChangeLog` tracks field-level diffs.
- `ProductHistory` tracks event-level actions (submitted, approved, etc.).
- The PostgreSQL audit trigger provides a safety-net second layer of change logging.

**Current problems:**
1. The restore UI **does not currently exist** — the version snapshots are stored but there is no route or template to restore from them.
2. Storage: Full JSONB snapshots for every save will grow. With 10,000 products × 15 versions × 60KB average = ~9GB over time.

**Proposed version/comment architecture:**

```
Comment model (NEW):
  product_id → FK
  user_id → FK
  section (e.g. 'range_overview', 'sales_arguments')
  content: Text
  resolved: Boolean
  parent_id → FK (for threaded comments)
  created_at
```

**For version storage, adopt a hybrid approach:**
- Store a **full snapshot** only at workflow stage transitions (marketing_draft → pending_director_pis, etc.) — these are the critical restore points. This limits snapshots to ~5-6 per product.
- Store **JSON diffs (patches)** for all intermediate saves within a stage. Use Python `dictdiffer` library to compute minimal diffs.
- This reduces storage by roughly 80%.

**Restore flow:**
1. Director or Admin opens the Version History panel for a product.
2. Selects a version snapshot from the timeline.
3. A diff preview shows what will change.
4. On confirmation, the current `pis_data`/`spec_data` is replaced with the snapshot and a `ProductHistory` event is logged.

**Route to add:** `POST /product/<id>/restore/<version_num>`

---

### 5.3 GDPR & Security Compliance

**Current gaps:**

| Area | Gap | Fix |
|---|---|---|
| **Authentication** | Cookie-based session with no MFA | Add TOTP MFA (e.g., `pyotp`) for admin accounts |
| **Session security** | `FLASK_SECRET_KEY` is in `.env` - fine, but sessions have no expiry | Add `PERMANENT_SESSION_LIFETIME = timedelta(hours=8)` |
| **CSRF** | No CSRF tokens on forms | Add `Flask-WTF` with `CSRFProtect` — all POST forms need `{{ form.csrf_token }}` |
| **Password policy** | No minimum password strength enforced | Add password strength validation on registration/reset |
| **Audit log retention** | `FieldChangeLog` has no expiry | GDPR Article 5(1)(e): define a retention period (e.g. 2 years) and a scheduled cleanup job |
| **Right to erasure** | No user data deletion mechanism | Admin must be able to anonymise a user (name, email → NULL, keep audit trail with user_id NULL) |
| **Data export** | No user data export | Add a "Download my data" endpoint for GDPR Article 20 |
| **HTTPS** | Not enforced in app | Force HTTPS via Azure load balancer + `PREFERRED_URL_SCHEME = 'https'` in flask config |
| **Rate limiting** | No rate limiting on login or API endpoints | Add `Flask-Limiter` on `/login` (e.g. 5 requests/minute) and AI generation endpoints |
| **Input sanitisation** | `safe` filter used in Jinja2 for trusted data | Audit all `| safe` usages — only use on admin-controlled content |
| **Secret key rotation** | No mechanism | Document key rotation procedure in ops runbook |

---

### 5.4 AI Prompt Refinement for Kalachand

**Current problems with generated content:**
1. The current prompts allow "long dashes" (em dashes `—`) and generic marketing language.
2. The `range_overview` prompt asks for "2-4 paragraphs" but does not specify paragraph structure — the AI often produces bullet-point-style text inside paragraphs or uses em dashes as separators.
3. The spec sheet generation does not specify Kalachand's tone of voice.

**Proposed prompt standards for Kalachand:**

```
TONE OF VOICE:
- Write in professional, clear British English (Mauritius uses British English standards).
- Avoid em dashes (—) entirely. Use commas or restructure the sentence.
- Do NOT use bullet points within paragraph text.
- Each paragraph must be 3-4 complete sentences minimum.
- Write in third person for product descriptions ("This iron features...", not "You will love...").
- Do not use superlatives without substantiation ("best", "most advanced") unless supported by spec data.

STRUCTURE FOR RANGE_OVERVIEW:
  Paragraph 1: Product identity — what it is, the brand, the primary use case.
  Paragraph 2: Key technology and performance differentiators (from tech specs).
  Paragraph 3: User benefits and experience.
  Paragraph 4 (if applicable): Warranty, service, and availability context.
```

---

### 5.5 Forbidden Words UI Improvements

**Current state:**
- The forbidden words page is **organised by the full 3-level product category tree** (Cat A → Cat B → Cat C).
- Users must navigate through 3 levels of tabs/accordions to find the right category before adding a word.
- The category tree has 134+ combinations — this makes the UI overwhelming for the Web Team.

**Problems:**
1. The accordion UI requires too many clicks to add a word to a specific category.
2. There is no way to see words sorted by "most recently added" or "most frequently triggered."
3. There is no way to add a **global** forbidden word (applies to all categories).
4. No bulk import (e.g., paste a comma-separated list).

**Proposed UX redesign:**

```
┌─────────────────────────────────────────────────────┐
│  FORBIDDEN WORDS                           + Add Word│
├──────────────┬──────────────────────────────────────┤
│ Quick Filters│  🔍 Search all words...               │
│ ─────────── │  ─────────────────────────────────────│
│ ● All Words │  GLOBAL WORDS (all categories)        │
│ ○ Global    │  [cheap] [inferior] [poor] [+]        │
│ ○ My Recent │  ─────────────────────────────────────│
│             │  CATEGORY-SPECIFIC                    │
│ CATEGORIES  │  Electronics > Kitchen > Blenders     │
│ Electronics │  [noisy] [unstable] [+]               │
│ Furniture   │  ─────────────────────────────────────│
│ ...         │  Furniture > Bedroom > Mattresses     │
│             │  [cheap foam] [+]                     │
└──────────────┴──────────────────────────────────────┘
```

**Quick Add modal:**
- A single "Add Word" button opens a modal with:
  - Word input field
  - Category selector (searchable dropdown)
  - "Apply to all categories" toggle
  - Bulk paste option (comma-separated)

---

### 5.6 Proforma Intelligence — 3-Tier AI Processing

This is the most complex new feature. The AI needs to classify each uploaded proforma and apply different extraction strategies.

**The 3 Scenarios (current state of support):**

| Scenario | Description | Current Support | Gap |
|---|---|---|---|
| **1:1** | 1 proforma = 1 product = 1 PIS | ✅ `generate_pis_data()` | None |
| **1:N Same** | 1 proforma = variants of 1 product (colours, sizes) → 1 PIS | ❌ No detection | Need classifier |
| **1:N Different** | 1 proforma = multiple different products → multiple PIS | ⚠️ `generate_bulk_pis_data()` | Works but no pre-classifier |

**The 3 Content Tiers:**

| Tier | Condition | Strategy | Current Support |
|---|---|---|---|
| **Tier 1** | Rich proforma (full specs, images, descriptions) | Extract directly from PDF | ✅ Existing flow |
| **Tier 2** | Sparse proforma (only product title/model number) | AI performs deep web research using DuckDuckGo + Jina scraping | ⚠️ Manual only, not an automated path |
| **Tier 3** | Kalachand private label (e.g., "Belair") | AI deduces specifications from category knowledge and similar products | ❌ Not implemented |

**Proposed 3-Tier Intelligence System:**

```
STEP 1 — Pre-processing Classifier (runs when PDF is uploaded)
  ↓ AI reads the proforma and answers:
  {
    "scenario": "1:1" | "1:N_variants" | "1:N_different",
    "products": [{"model": "...", "richness": "full" | "sparse" | "private_label"}],
    "suggested_strategy": "..."
  }

STEP 2 — Strategy Router
  if scenario == "1:1":
    → single product extraction (existing)
  if scenario == "1:N_variants":
    → extract ONE PIS, populate variant table (colour, size) as additional fields
  if scenario == "1:N_different":
    → bulk extraction (existing generate_bulk_pis_data)

STEP 3 — Enrichment by Tier
  Tier 1 (rich): Direct extraction from PDF
  Tier 2 (sparse): Deep web research
    → scrape_url_data_deep() using model name as search query
    → DuckDuckGo search for "{model_name} specifications"
  Tier 3 (Kalachand brand): Deduction mode
    → use product category + visual inspection of PDF
    → prompt: "Based on the product category and any available visual/descriptive context, 
       suggest realistic and accurate specifications typical for this product type 
       in the Mauritius market. Clearly mark deduced fields as [SUGGESTED]."
```

**New prompt needed (`proforma_classifier`):**
```
You are a product document analyst.
Analyse the uploaded proforma invoice/specification document.

Determine:
1. How many distinct products are present?
2. Are they the same product in different variations (colours, sizes) or entirely different products?
3. For each product, assess how much information is available (rich, sparse, or a private-label brand).

Output strictly valid JSON:
{
  "scenario": "1:1" | "1:N_variants" | "1:N_different",
  "product_count": integer,
  "products": [
    {
      "model": "...",
      "brand": "...",
      "richness": "full" | "sparse" | "private_label",
      "notes": "..."
    }
  ],
  "recommended_strategy": "..."
}
```

---

## 6. Summary: Recommended Priorities

### Phase 1 — Stability & Testability (Before Azure Deployment)
- [ ] Refactor `app.py` into Flask Blueprints
- [ ] Move prompts to PostgreSQL (`Prompt` table)
- [ ] Move jobs to PostgreSQL (`Job` table)
- [ ] Add CSRF protection (`Flask-WTF`)
- [ ] Add session expiry + rate limiting on login
- [ ] Set up `tests/` with basic pytest suite (auth, workflow transitions)
- [ ] Set up Azure Blob Storage for image uploads
- [ ] Configure Playwright `--no-sandbox` + `--disable-dev-shm-usage` in Docker

### Phase 2 — New Features (Core)
- [ ] Implement version restore UI (`/product/<id>/restore/<version>`)
- [ ] Add Comment model and inline comment UI per product section
- [ ] Implement proforma classifier (Scenario 1/2/3 + Tier 1/2/3)
- [ ] Redesign forbidden words UI (single modal, global words, bulk add)
- [ ] Refine all AI prompts for Kalachand tone (no em dashes, structured paragraphs)

### Phase 3 — Compliance & Polish
- [ ] GDPR: user anonymisation, data export, retention policy
- [ ] MFA for admin accounts
- [ ] Azure CI/CD pipeline (GitHub Actions → Azure Container Apps)
- [ ] Performance: add Redis cache for category tree and frequent DB reads
- [ ] UI polish: keyboard shortcuts, sticky action bars, mobile sidebar fix

---

## 7. File Map of What Needs to Change

```
REFACTOR (Major):
  app.py → split into app/__init__.py + blueprints/

ADD (New files):
  app/blueprints/auth/routes.py
  app/blueprints/marketing/routes.py
  app/blueprints/director/routes.py
  app/blueprints/web/routes.py
  app/blueprints/admin/routes.py
  app/blueprints/api/routes.py
  utils/storage.py              (Azure Blob Storage helper)
  utils/job_queue.py            (PostgreSQL-backed job queue)
  tests/conftest.py
  tests/unit/test_models.py
  tests/unit/test_ai_generation.py
  tests/integration/test_auth_routes.py

MODIFY:
  model.py → add Job, Comment, Prompt models; soft-delete on Product
  utils/prompt_manager.py → read/write from DB instead of JSON
  utils/ai_generation.py → add proforma_classifier function
  utils/pdf_processing.py → add --no-sandbox to Playwright args
  Dockerfile → confirm ENV vars for Azure
  requirements.txt → add flask-wtf, flask-limiter, azure-storage-blob, dictdiffer

REMOVE:
  data/system_prompts.json → migrated to DB
  data/pis_jobs.json → migrated to DB
  implementation_plan.m → was a typo file
```

---

*Document version 1.0 — to be updated as features are implemented.*