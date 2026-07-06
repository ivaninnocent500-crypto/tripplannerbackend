package com.tta.africasafariguide

import kotlinx.coroutines.delay

/**
 * Tries live GitHub-hosted operator data first; falls back to a small local
 * mock list if the network fetch fails (offline, GitHub down, bad URL, etc.)
 * so the app never shows an empty operator list due to a transient error.
 *
 * CAVEAT (repeat from GitHubOperatorDataSource): this is remotely-editable
 * mock data, not real-time inventory or availability. It is a real
 * improvement over a hardcoded-in-APK list, but it is not "live booking
 * data" in the sense of a GDS or operator reservation system.
 */
class OperatorRepository {

    private data class OperatorBase(
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

    // Local fallback only — used if the GitHub fetch fails.
    private val fallbackDb = listOf(
        OperatorBase("id_1", "Serengeti Elite Ltd", 4.9, 324, 3750, listOf("Tanzania")),
        OperatorBase("id_2", "Nomad Kenya", 4.8, 156, 4100, listOf("Kenya")),
        OperatorBase("id_3", "Great Rift Safaris", 4.7, 89, 2900, listOf("Tanzania", "Kenya"))
    )

    suspend fun getMatches(
        country: String,
        budget: Double,
        parks: List<String>,
        forceRefresh: Boolean = false
    ): List<SafariOperator> {
        val remote = GitHubOperatorDataSource.fetchOperators(forceRefresh)

        val remoteFiltered = remote?.filter { it.countries.contains(country) }

        if (remoteFiltered != null) {
            return remoteFiltered.map { candidate ->
                val score = computeMatchScore(
                    basePrice = candidate.basePrice,
                    rating = candidate.rating,
                    reviews = candidate.reviews,
                    budget = budget
                )
                candidate.toSafariOperator(score)
            }.sortedByDescending { it.matchScore }
        }

        // Network fetch failed entirely (not even stale cache available) —
        // fall back to the local mock list so the app still shows something.
        delay(800) // Simulate network for the local fallback path
        return fallbackDb
            .filter { it.countries.contains(country) }
            .map { base ->
                SafariOperator(
                    id = base.id,
                    name = base.name,
                    rating = base.rating,
                    reviews = base.reviews,
                    basePrice = base.basePrice,
                    matchScore = computeMatchScore(
                        basePrice = base.basePrice,
                        rating = base.rating,
                        reviews = base.reviews,
                        budget = budget
                    ),
                    countries = base.countries,
                    vehicleType = base.vehicleType,
                    allInclusive = base.allInclusive,
                    airportTransfer = base.airportTransfer
                )
            }
            .sortedByDescending { it.matchScore }
    }

    /**
     * Real, explainable match scoring. Weighted factors:
     * - Budget fit (40%): how close basePrice is to the user's stated budget
     *   without exceeding it; over-budget is penalized more than under-budget.
     * - Rating (35%): normalized against a 5.0 scale.
     * - Review volume (25%): log-scaled so 300 reviews isn't "10x more
     *   trustworthy" than 30 — just meaningfully more.
     *
     * Exposed with clear inputs/outputs so a future "why X%?" UI can call
     * this same logic and show the three components, instead of presenting
     * an unexplainable single number.
     */
    private fun computeMatchScore(
        basePrice: Int,
        rating: Double,
        reviews: Int,
        budget: Double
    ): Int {
        val budgetScore = when {
            basePrice > budget -> {
                val overBy = (basePrice - budget) / budget
                (1.0 - overBy).coerceIn(0.0, 1.0)
            }
            else -> {
                val underBy = (budget - basePrice) / budget
                (1.0 - (underBy * 0.3)).coerceIn(0.7, 1.0)
            }
        }

        val ratingScore = (rating / 5.0).coerceIn(0.0, 1.0)

        val reviewScore = (Math.log10((reviews + 1).toDouble()) / Math.log10(500.0))
            .coerceIn(0.0, 1.0)

        val weighted = (budgetScore * 0.40) + (ratingScore * 0.35) + (reviewScore * 0.25)

        return (weighted * 100).toInt().coerceIn(0, 100)
    }
}
