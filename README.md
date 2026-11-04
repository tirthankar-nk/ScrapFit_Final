# ScrapFit (upgraded, local-JSON phase)

A Streamlit app that helps people find leftover wood and metal for a project, and lets
sellers list their offcuts. This version adds **Guest Mode, local accounts, quality grades,
availability and quantity management, QR codes, advanced search and a more explainable
matching engine**, while still storing everything in your existing `inventory.json`.

> **Phase note.** Supabase (online database, authentication, storage, Row Level Security,
> e-mail password recovery) is deliberately **not** part of this phase. See
> [Preparing for Supabase](#preparing-for-supabase-next-phase).

---

## 1. Quick start

```bash
# 1. Put your EXISTING files next to app.py (see "Using your existing data" below)
# 2. Install
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Run
streamlit run app.py
```

Open the address Streamlit prints (usually http://localhost:8501).

### Using your existing data (important)

Copy these from your old project folder into this folder **before first run**:

| Copy this | Why |
|---|---|
| `inventory.json` | your listings (IDs, prices, dimensions, sellers...) |
| `scrapfit_images/` | photos your sellers uploaded |
| `scrapfit_hero_photo.png` | the hero picture on the Home page |

On first start ScrapFit notices an old-format `inventory.json`, **makes a timestamped
backup in `backups/` first**, then adds the new fields. Nothing is deleted or overwritten.
You will see a message telling you where the backup is. If there is no `inventory.json`,
the original 7 demo listings (S001–S007) are created.

Do not unzip this project over an old folder that holds your data without copying
`inventory.json` out first. (This download deliberately contains no `inventory.json`.)

You can also upgrade manually and see a report:

```bash
python tools/upgrade_inventory.py --dry-run     # report only, changes nothing
python tools/upgrade_inventory.py               # backup + upgrade
python tools/upgrade_inventory.py --backup-only # just a backup copy
```

---

## 2. What changed

**Guest Mode.** The first screen offers *Continue as Guest*, *Log in* and *Create Account*.
Guests can browse, search, filter, use the Project Planner and Project Ideas, view
details/photos/seller contact, open public links and scanned QR codes. Guests cannot create,
edit or delete listings or keep permanent favourites. They see a friendly prompt instead.
(Guests can still tick "Save for Later" for the current visit, as in the original app; it moves
into their account if they log in.)

**Accounts (local).** Register (name, email, password + confirmation, optional phone), log in,
log out, edit profile, change password. Passwords are stored only as salted PBKDF2-SHA256
hashes in `users.json`. After 5 wrong passwords an account is locked for 5 minutes.

**Listings.** Quality grades A/B/C (descriptions shown in forms and cards), availability
(Available / Reserved / Sold / Unavailable), quantity, material type, multiple photos,
created/updated dates. Owners can edit everything, replace photos, mark "Sold 1 piece"
(quantity 4 → 3; at 0 the listing becomes Sold), and delete. Only Available listings with
quantity ≥ 1 appear in normal search and project matching.

**Ownership.** Every new listing stores its owner. Edit/delete/quantity changes are checked in
`scrapfit/data_store.py`, not just by hiding buttons.

**QR codes.** Each listing has a permanent ID. In *Inventory → Manage this listing → QR code* you
can see and download a PNG. It encodes `<APP_BASE_URL>/?listing=<ID>`, which opens the listing
page. Editing a listing never changes its ID, so printed codes keep working.

**Find Scrap.** Filters combine (category, type, grade, availability, price, pickup place,
length/width/thickness ranges, required part dimensions). Sort by lowest/highest price, best
dimensional fit, best quality, recently added, nearest location.

**Matching.** Same shelf-packing allocator as before, now with: minimum quality, availability
and quantity rules, per-match explanations ("why this listing matches"), and for unmatched parts
a list of reasons (wrong thickness, too small, below minimum grade, pieces already used…).

---

## 3. Configure the public URL for QR codes

QR codes must point at the **deployed** address, not `localhost`. Set it in either place:

1. `.streamlit/secrets.toml`
   ```toml
   APP_BASE_URL = "https://your-app-name.streamlit.app"
   ```
2. or an environment variable: `export SCRAPFIT_BASE_URL="https://your-app-name.streamlit.app"`

Templates: `.streamlit/secrets.toml.example` and `.env.example` (placeholders only).
Streamlit does **not** read `.env` by itself; use secrets.toml or export the variable.
Until it is set, the QR tab shows a warning and the code only works on your own computer.

Optional: `ADMIN_EMAILS` / `SCRAPFIT_ADMIN_EMAILS` (comma separated) makes those accounts admins:
they may claim ownerless old listings and get backup/export buttons on the Account page.

---

## 4. Older listings and ownership

Listings created before accounts existed have no owner. ScrapFit **does not guess** one. They are
flagged (`owner_review_needed: true`) and are read-only until claimed:

* an account whose **profile phone number matches the listing's contact number** can claim it
  (Inventory page or Account → "Older listings you can claim"), or
* an admin account can claim any ownerless listing.

So: register with the same phone number you used on your old listings, then claim them.

---

## 5. Backup and recovery

| What | Where |
|---|---|
| Timestamped copy before any upgrade | `backups/inventory_pre_upgrade_<time>.json` |
| Rolling copy of the previous state, refreshed on every save | `inventory.json.bak` |
| Manual backup | `python tools/upgrade_inventory.py --backup-only` or admin button |
| Invalid entries found during upgrade | `backups/invalid_records_<time>.json` |
| Original app code | `legacy/app_original.py` |

**Restore:** stop the app, copy the backup over `inventory.json`
(e.g. `cp backups/inventory_pre_upgrade_20261103_101500.json inventory.json`), start the app.
Also back up `scrapfit_images/` and `users.json` if you want photos and accounts preserved.
If `inventory.json` ever cannot be read, the app shows an error and refuses to save, so a broken
file is never overwritten.

---

## 6. Project layout

```
app.py                      router: startup, welcome gate, sidebar, page routing
scrapfit/
  config.py                 paths, constants, public URL
  data_store.py             inventory.json repository, normalisation, validation, ownership
  auth.py                   local accounts (users.json), password hashing
  matching.py               fit checks, allocation, explanations, cost estimate
  search.py                 filters and sorting for Find Scrap
  qr_utils.py               listing URLs and QR PNGs
  photos.py                 photo validation/saving
  session.py                Streamlit helpers: current user, guest mode, flash messages
  ui_components.py          the single listing-card renderer used on every page
  theme.py                  original CSS (unchanged) + new badge/responsive CSS
  stock_images.py, sample_data.py, ai_draft.py   moved unchanged from the original app.py
  views/                    one file per page
tools/upgrade_inventory.py  upgrade / dry-run / backup CLI
tests/                      automated tests (see below)
sample_data/inventory.sample.json   demo data in the new format
legacy/app_original.py      your original file, for reference/rollback
```

New fields on each listing (old ones are all kept): `quality_grade`, `material_type`, `owner_id`,
`owner_review_needed`, `created_at`, `updated_at`, `photos` (list; `image_file` still mirrors the
first photo for old code), `schema_version`, and `review_notes` where a default was a guess.
The old `condition` text is still written. The old status "Sold / Unavailable" cannot be split
into Sold vs Unavailable, so it became **Unavailable** (the original value is kept in `legacy_status`).
Old records without a grade get **Grade C** (the most cautious) plus a review note.

---

## 7. Testing

```bash
python -m unittest discover -s tests -v     # all automated tests
python tests/ui_smoke_runner.py             # UI walkthrough only
python tools/check_names.py                 # undefined-name check
```

**What the tests cover (98 tests, all passing when this was delivered)**
* Data: old→new upgrade keeps every value; idempotent; backup; corrupt file never overwritten; duplicate IDs; invalid records quarantined.
* Ownership: guests and other users cannot create/edit/delete/change quantity; protected fields (`id`, `owner_id`, dates) cannot be changed; claiming rules.
* Quantity: 4 → 3 on sale, never below 0, 0 → Sold, restock → Available.
* Matching: exact fit, 1 mm short, rotation, thickness/material, Reserved/Sold/Unavailable/0-quantity excluded, minimum grade, quantity = separate physical pieces never reused, shared pieces never exceed their area (200 random cases), smallest offcut preferred, explanations.
* **Regression:** the new matcher gives identical results to your original `allocate_project_parts` and `find_matches` on 500 random inputs (`tests/legacy_matching_reference.py` is your original code, verbatim).
* Search: combined filters, every sort option, distance sorting ignores the old default demo pin.
* Auth: registration validation, duplicate email, wrong password, lockout, no plain-text passwords, per-user saved listings.
* QR: URL uses the configured base URL and is identical before/after editing a listing.
* UI walkthrough (57 checks): guest browsing, search and planner, registration, login/logout, create/edit/delete listing, quantity, QR section, claim, photo upload/replace/delete, public listing link, unknown ID.

**What was NOT tested here — please check these yourself**
* The UI walkthrough ran against a *stand-in* for Streamlit (`tests/fake_streamlit.py`) because Streamlit could not be installed in the build environment. It checks that code paths run and reach the data layer, and enforces two real Streamlit rules (no duplicate widget keys, no changing widget state after creation). **It does not prove how the pages look or behave in a real browser.** Run `streamlit run app.py` and click through every page once.
* **QR scanning.** The `qrcode` package was not installed either, so PNG generation was tested with a stand-in library. After `pip install -r requirements.txt`, download one QR, scan it with a phone and confirm it opens the listing. (Use a deployed URL, or your computer's LAN address, for a phone to reach it.)
* Responsive layout on phone/tablet (CSS was added but not viewed on devices), and the optional Gemini AI draft feature (needs your API key).
* Everything Supabase-related (not built in this phase).

---

## 8. Deploying (and an important warning)

Streamlit Community Cloud (or any host) can run this app, but **local JSON files are not safe
storage on most hosts**: the disk is typically reset on restart/redeploy, so listings, accounts and
photos would be lost. Use local deployment for demos only. Durable public use needs the Supabase
phase. If you still deploy to demo it: push the code (the `.gitignore` excludes your data files, so
the app will start with demo listings), set `APP_BASE_URL` in the host's *Secrets*, then reboot.
This README does not claim the app is deployed anywhere.

---

## 9. Preparing for Supabase (next phase)

The UI talks only to `JsonListingRepository` / `JsonUserRepository` and `photos.py`. The later move is
to write Supabase versions with the same method names:

| Today | Supabase phase |
|---|---|
| `JsonListingRepository` (`list_all/get/create/update/delete/adjust_quantity/claim`) | `listings` table; ownership via RLS (`owner_id = auth.uid()`) |
| `JsonUserRepository` + `users.json` | Supabase Auth + `profiles` table; e-mail password recovery |
| `saved_ids` inside users.json | `saved_listings(user_id, listing_id, saved_at)` |
| `photos.py` + `scrapfit_images/` | Supabase Storage bucket; `photos` holds storage paths |
| `normalise_record()` + `upgrade_inventory.py` | basis for the JSON → database import script |
| `owner_review_needed` | rows imported with `owner_id = NULL` pending review |

Not done now, by design: SQL schema, RLS policies, migration script, Supabase Auth, cloud
storage, password-reset e-mails.

---

## 10. Known limitations

* **Local accounts are for development.** No e-mail verification and **no password recovery** (needs e-mail, planned with Supabase Auth). A forgotten password currently means editing/removing the account in `users.json`.
* Login lasts for the browser session; refreshing the page logs you out (a secure persistent login needs the online auth phase).
* Ownership is enforced in Python, not by a database. Anyone with file access to `inventory.json` can still edit it. Database-level protection (RLS) comes with Supabase.
* JSON files: fine for one machine and a few hundred listings; a lock protects against simultaneous edits in one process only.
* Matching is a transparent heuristic, **not** a globally optimal cutting plan; it ignores blade kerf and grain; thickness must match exactly.
* "Nearest location" uses straight-line distance and only listings with a real map pin (the old default demo pin is ignored). Most older listings have none.
* The map still shows illustrative pins for listings without coordinates (as before).
* Listing edit does not change the map pin.
