package com.tta.africasafariguide

import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL

/**
 * Fetches operator data from a JSON file hosted in a GitHub repo via the
 * raw.githubusercontent.com URL (no auth needed for public repos, no GitHub
 * API rate limits since this bypasses the REST API entirely).
 *
 * CAVEAT: this is remotely-editable mock data, not live inventory or
 * real-time availability. It's a real improvement over data hardcoded into
 * the APK (editable without a rebuild/release), but it is not a booking
 * feed. Treat it as a stepping stone toward real operator API integrations.
 */
object GitHubOperatorDataSource {
    private const val TAG = "GitHubOperatorDataSource"

    // ⚠️ PLACEHOLDER URL — replace with your real repo before shipping.
    // Format: https://raw.githubusercontent.com/{user}/{repo}/{branch}/{path}
    // Example once real: https://raw.githubusercontent.com/tta-safari/safari-data/main/operators.json
    private const val DATA_URL =
        "https://raw.githubusercontent.com/REPLACE_ME_ORG/REPLACE_ME_REPO/main/operators.json"

    private const val CACHE_TTL_MS = 15 * 60 * 1000L // 15 minutes

    data class RemoteOperator(
        val id: String,
        val name: String,
        val rating: Double,
        val reviews: Int,
        val basePrice: Int,
        val countries: List<String>,
        val vehicleType: String = "Standard 4x4",
        val allInclusive: Boolean = false,
        val airportTransfer: String = "Shared"
    )

    private val mutex = Mutex()
    private var cachedData: List<RemoteOperator>? = null
    private var cachedAtMs: Long = 0L

    /**
     * Fetches operator data, using an in-memory cache to avoid hitting
     * GitHub on every screen visit. Cache is process-lifetime only (cleared
     * on app kill) — fine for pricing data that doesn't need to survive
     * restarts, and avoids the complexity of disk-backed caching for now.
     *
     * Returns null only if there's no cached data AND the network fetch
     * fails — callers should fall back to local mock data in that case.
     */
    suspend fun fetchOperators(forceRefresh: Boolean = false): List<RemoteOperator>? =
        mutex.withLock {
            val cacheAge = System.currentTimeMillis() - cachedAtMs
            val cacheIsFresh = cachedData != null && cacheAge < CACHE_TTL_MS

            if (!forceRefresh && cacheIsFresh) {
                Log.d(TAG, "Serving operators from cache (age: ${cacheAge / 1000}s)")
                return@withLock cachedData
            }

            val fetched = fetchFromNetwork()
            if (fetched != null) {
                cachedData = fetched
                cachedAtMs = System.currentTimeMillis()
                Log.d(TAG, "Refreshed operator cache from network (${fetched.size} operators)")
                fetched
            } else if (cachedData != null) {
                Log.w(TAG, "Network fetch failed, serving stale cache")
                cachedData
            } else {
                Log.e(TAG, "Network fetch failed and no cache available")
                null
            }
        }

    private suspend fun fetchFromNetwork(): List<RemoteOperator>? = withContext(Dispatchers.IO) {
        try {
            val connection = URL(DATA_URL).openConnection() as HttpURLConnection
            connection.connectTimeout = 8000
            connection.readTimeout = 8000
            connection.requestMethod = "GET"

            if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                Log.e(TAG, "GitHub fetch failed: HTTP ${connection.responseCode}")
                return@withContext null
            }

            val json = connection.inputStream.bufferedReader().use { it.readText() }
            val type = object : TypeToken<List<RemoteOperator>>() {}.type
            Gson().fromJson<List<RemoteOperator>>(json, type)
        } catch (e: IOException) {
            Log.e(TAG, "Network error fetching operator data", e)
            null
        } catch (e: Exception) {
            Log.e(TAG, "Error parsing operator data", e)
            null
        }
    }
}

fun GitHubOperatorDataSource.RemoteOperator.toSafariOperator(matchScore: Int): SafariOperator =
    SafariOperator(
        id = id,
        name = name,
        rating = rating,
        reviews = reviews,
        basePrice = basePrice,
        matchScore = matchScore,
        countries = countries,
        vehicleType = vehicleType,
        allInclusive = allInclusive,
        airportTransfer = airportTransfer
    )
