# Africa Safari Guide — Fixes Delivery

This package contains every file changed or added across this conversation,
in full, ready to drop into your project. Nothing here is partial —
every `.kt` file is complete and compiles against your confirmed real data
models (`SafariOperator`, `BookingRequest`, `AppDatabase`, etc.).

## ⚠️ Do this first, before anything else

The Gemini API key `AIzaSyBCsSTqybuOe61NwU0wpmKmUSzU4I_FvBs` has appeared
multiple times across screenshots and pasted code in this conversation.
**Revoke it now** at https://aistudio.google.com/app/apikey and generate a
new one. Every fix below assumes a fresh key.

## Folder structure

```
android/                          — Kotlin files, copy into your app module
  com/tta/africasafariguide/
    ApiKeyProvider.kt              — NEW: single source of truth for the API key
    GeminiFlashService.kt          — FIXED: no hardcoded key
    RealAIPackingService.kt        — FIXED: no hardcoded key
    GitHubOperatorDataSource.kt    — NEW: remote operator data + caching
    OperatorRepository.kt          — FIXED: real match scoring, not hardcoded
    ItineraryViewModel.kt          — FIXED: real share, caching wired, backend sync
    InquiryApiClient.kt            — NEW: talks to the backend
    PremiumItineraryScreen.kt      — FIXED: real share button, honest inquiry copy
    UiStates.kt                    — your existing file, included for completeness
    Models.kt                      — your existing file, included for completeness
    data/
      Entities.kt                  — your existing Room entities, unmodified
      AppDatabase.kt                — your existing Room database, unmodified
  AndroidManifest.xml              — FIXED: removed duplicate FileProvider
  build.gradle.kts.snippet.txt     — ADD: BuildConfig key injection
  local.properties.template.txt   — REFERENCE: correct format

backend/                          — Complete, runnable Node.js service
  src/
    server.js
    db/database.js
    routes/inquiries.js
    middleware/adminAuth.js
  package.json
  .env.example
  .gitignore
  README.md                        — Full setup + deploy instructions

github-data-sample/
  operators.json                   — Upload this to your GitHub data repo
```

## Integration steps, in order

1. **Rotate the Gemini key** (see warning above).

2. **Copy the Android files** into
   `app/src/main/java/com/tta/africasafariguide/` (and the `data/`
   subfolder), overwriting your existing versions of files with the same
   name.

3. **Update `app/build.gradle.kts`** using `build.gradle.kts.snippet.txt` —
   merge its `android { }` additions into your existing file rather than
   replacing the whole file, since your real `build.gradle.kts` has other
   content (dependencies, etc.) not shown in this conversation.

4. **Update `local.properties`** — add the `GEMINI_API_KEY=` line from
   `local.properties.template.txt`, using your new rotated key.

5. **Replace the manifest** — `AndroidManifest.xml` here has the duplicate
   `FileProvider` block removed; use it as-is or apply the same removal to
   your file if it has other content added since you last shared it.

6. **Set up the GitHub data repo**:
   - Create a repo (public is simplest).
   - Upload `github-data-sample/operators.json` to it.
   - In `GitHubOperatorDataSource.kt`, replace:
     ```kotlin
     private const val DATA_URL =
         "https://raw.githubusercontent.com/REPLACE_ME_ORG/REPLACE_ME_REPO/main/operators.json"
     ```
     with your real repo's raw URL.

7. **Deploy the backend** — follow `backend/README.md` (Render/Railway/
   Fly.io all work, free tiers available). Takes about 10 minutes.

8. **Wire the backend URL into the app** — in `InquiryApiClient.kt`,
   replace:
   ```kotlin
   private const val BACKEND_BASE_URL = "https://REPLACE_ME_WITH_YOUR_BACKEND_URL"
   ```
   with your deployed URL.

9. **Build and test**:
   - Generate a trip → confirm operators load (from GitHub data, with
     real computed match scores, not hardcoded 92%/85%).
   - Tap "GET QUOTE" on an operator → confirm the dialog says "REQUEST A
     QUOTE" and the button says "SEND INQUIRY", not "BOOK NOW"/"CONFIRM
     BOOKING".
   - Submit an inquiry → confirm it appears via
     `GET /api/inquiries` on your deployed backend (with your
     `ADMIN_API_KEY` in the Authorization header).
   - Tap Share on a generated itinerary → confirm Android's native share
     sheet opens with the itinerary text.

## What is intentionally NOT included

- **Real payment processing.** The backend's README explains exactly where
  to add Stripe later, and why it should not be wired into the initial
  inquiry-submission endpoint.
- **Real operator inventory/availability APIs.** The GitHub-hosted JSON is
  a real improvement over data hardcoded into the compiled app (editable
  without a rebuild), but it is still manually-maintained data, not a live
  booking feed from SafariBookings, Amadeus, or similar. That is a larger,
  separate integration effort.
- **A rename of `BookingState`/`BookingEntity`/`bookingDao` to
  `Inquiry*`-prefixed names.** The user-facing copy is now honest
  ("SEND INQUIRY", "REQUEST A QUOTE"), but the internal Kotlin/Room type
  names were left as `Booking*` to avoid a Room schema migration. Renaming
  those too is a reasonable follow-up but touches your database schema
  version and migration path, which needs its own careful pass.
