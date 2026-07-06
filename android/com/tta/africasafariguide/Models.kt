package com.tta.africasafariguide

import androidx.annotation.Keep

data class ChatMessage(
    val text: String,
    val isUser: Boolean
)

data class FAQ(
    val question: String
)

@Keep
data class SafariItinerary(
    val title: String = "Your Safari",
    val totalEstimatedCost: String = "Contact for Pricing",
    val highlights: List<String> = emptyList(),
    val costBreakdown: CostBreakdown? = null,
    val days: List<DailyPlan> = emptyList(),
    val primaryCountry: String? = null,
    val countryId: Int? = null,
    val parksVisited: List<String> = emptyList(),
    val travelAdvisory: String = "No advisories at this time."
)

@Keep
data class CostBreakdown(
    val transport: String = "Included",
    val accommodation: String = "Standard",
    val parkFees: String = "Variable",
    val meals: String = "Full Board"
)

@Keep
data class DailyPlan(
    val day: Int = 1,
    val location: String = "Unknown Location",
    val activities: List<String> = emptyList(),
    val latitude: Double = 0.0,
    val longitude: Double = 0.0,
    val imageUrl: String? = null,
    val weatherIcon: String = "Sunny",
    val temperature: String = "--"
)

@Keep
data class SafariOperator(
    val id: String,
    val name: String,
    val rating: Double,
    val reviews: Int,
    val basePrice: Int,
    val matchScore: Int,
    val countries: List<String>,
    val vehicleType: String = "Standard 4x4",
    val allInclusive: Boolean = false,
    val airportTransfer: String = "Shared"
)

@Keep
data class PackingItem(
    val name: String,
    val reason: String,
    val category: String,
    val essential: Boolean = true
)

@Keep
data class BookingRequest(
    val operatorId: String,
    val operatorName: String,
    val itineraryTitle: String,
    val totalPrice: Int,
    val travelerName: String = "",
    val travelerEmail: String = "",
    val travelerPhone: String = "",
    val travelDate: String = "",
    val numberOfTravelers: Int = 1,
    val specialRequests: String = ""
)

// The UI State for the MVI pattern
data class EGuideUiState(
    val chatHistory: List<ChatMessage> = listOf(
        ChatMessage("Hello! I'm your expert African Safari E-Guide 🌍. How can I help today?", false)
    ),
    val isAgentThinking: Boolean = false,
    val faqs: List<FAQ> = listOf(
        FAQ("What are the 'Big Five'?"),
        FAQ("Best time for a Serengeti safari?"),
        FAQ("Wildlife migration dates 2026")
    )
)
