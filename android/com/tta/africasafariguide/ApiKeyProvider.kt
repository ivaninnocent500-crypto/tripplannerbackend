package com.tta.africasafariguide

/**
 * Single source of truth for the Gemini API key.
 *
 * The key itself lives ONLY in local.properties (gitignored) and is injected
 * into BuildConfig at compile time via app/build.gradle.kts. No file in this
 * package should ever contain the literal key string again.
 *
 * NOTE: This still ships inside the compiled APK as a BuildConfig constant,
 * which means it CAN be extracted via decompilation. This protects source
 * control and code review, not the shipped binary. The backend proxy in
 * this same delivery (see /backend) is the fix that also protects the
 * binary — route Gemini calls through your own server once that is live,
 * instead of calling Google directly from the client.
 */
object ApiKeyProvider {
    val geminiApiKey: String by lazy {
        val key = BuildConfig.GEMINI_API_KEY
        require(key.isNotBlank()) {
            "GEMINI_API_KEY is missing. Add it to local.properties as " +
            "GEMINI_API_KEY=your_key_here (no quotes), then rebuild."
        }
        key
    }
}
