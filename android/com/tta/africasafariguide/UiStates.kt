package com.tta.africasafariguide

// Sealed UI States for Error Resilience

// SEALED STATES - Declared ONCE here
sealed class ItineraryUiState {
    // FIX: Initialized with an empty list and made 'open' so subclasses can override it if needed.
    open val alerts: List<String> = emptyList()

    object Idle : ItineraryUiState()
    object Loading : ItineraryUiState()

    // Note: If you want 'visaAlerts' to act as the main 'alerts' for the Success state,
    // you can override it inside this data class body.
    data class Success(val itinerary: SafariItinerary, val visaAlerts: List<String>) : ItineraryUiState()

    data class Error(val message: String) : ItineraryUiState()
}

sealed class OperatorUiState {
    object Loading : OperatorUiState()
    data class Success(val operators: List<SafariOperator>) : OperatorUiState()
    data class Error(val message: String) : OperatorUiState()
}

sealed class InquiryUiState {
    object Idle : InquiryUiState()
    object Submitting : InquiryUiState()
    object Success : InquiryUiState()
    data class Error(val message: String) : InquiryUiState()
}


object PackingIntelligenceEngine {
    data class PackingItem(val name: String, val reason: String)

    fun generateList(country: String, parks: List<String>): List<PackingItem> {
        val list = mutableListOf(
            PackingItem("Passport & Visa", "Essential for $country entry."),
            PackingItem("Neutral Clothing", "Best for game drives and blending in.")
        )
        if (parks.any { it.contains("Serengeti", ignoreCase = true) }) {
            list.add(PackingItem("Binoculars", "Critical for long-distance predator spotting."))
        }
        if (parks.any { it.contains("Ngorongoro", ignoreCase = true) }) {
            list.add(PackingItem("Heavy Fleece", "The crater rim drops to 5°C at night."))
        }
        return list
    }
}

// Analytics Hook for Production Tracking
object AnalyticsTracker {
    fun logEvent(event: String, params: Map<String, String> = emptyMap()) {
        println("Analytics: $event | Params: $params")
    }
}
