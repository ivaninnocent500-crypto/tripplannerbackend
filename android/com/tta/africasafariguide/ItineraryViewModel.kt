package com.tta.africasafariguide

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.google.gson.Gson
import com.tta.africasafariguide.data.*
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import java.util.*

class ItineraryViewModel(
    application: Application,
    private val vaultDao: VaultDao,
    private val savedItineraryDao: SavedItineraryDao,
    private val bookingDao: BookingDao
) : AndroidViewModel(application) {

    private val operatorRepo = OperatorRepository()

    // State flows
    private val _uiState = MutableStateFlow<ItineraryUiState>(ItineraryUiState.Idle)
    val uiState: StateFlow<ItineraryUiState> = _uiState.asStateFlow()

    private val _operatorState = MutableStateFlow<OperatorUiState>(OperatorUiState.Loading)
    val operatorState: StateFlow<OperatorUiState> = _operatorState.asStateFlow()

    private val _selectedOperators = MutableStateFlow<List<SafariOperator>>(emptyList())
    val selectedOperators: StateFlow<List<SafariOperator>> = _selectedOperators.asStateFlow()

    private val _packingList = MutableStateFlow<List<PackingItem>>(emptyList())
    val packingList: StateFlow<List<PackingItem>> = _packingList.asStateFlow()

    private val _bookingState = MutableStateFlow<BookingState>(BookingState.Idle)
    val bookingState: StateFlow<BookingState> = _bookingState.asStateFlow()

    // One-shot event channel for share-sheet triggering. SharedFlow (not
    // StateFlow) because "share this" is an event, not persisted state — a
    // StateFlow would re-fire the share sheet on recomposition/rotation.
    private val _shareEvent = MutableSharedFlow<ShareItineraryPayload>(extraBufferCapacity = 1)
    val shareEvent: SharedFlow<ShareItineraryPayload> = _shareEvent.asSharedFlow()

    private var currentItinerary: SafariItinerary? = null

    // Vault entries
    val vaultEntries: StateFlow<List<VaultEntry>> = vaultDao.getVaultEntries()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    val savedItineraries: StateFlow<List<SavedItinerary>> = savedItineraryDao.getAllSavedItineraries()
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5000), emptyList())

    // Operator selection
    fun toggleOperatorSelection(operator: SafariOperator) {
        val current = _selectedOperators.value.toMutableList()
        if (current.any { it.id == operator.id }) {
            current.removeAll { it.id == operator.id }
        } else {
            if (current.size < 2) {
                current.add(operator)
            } else {
                current[1] = operator
            }
        }
        _selectedOperators.value = current
    }

    fun clearSelection() {
        _selectedOperators.value = emptyList()
    }

    // Generate itinerary
    fun generateTrip(userRequest: String, userNationality: String) {
        if (userRequest.isBlank()) return

        viewModelScope.launch {
            _uiState.value = ItineraryUiState.Loading
            try {
                val result = GeminiFlashService.generateSafari(userRequest)

                val daysWithPhotos = result.days.map { day ->
                    async {
                        val photo = UnsplashImageService.getPhotoForLocation(day.location)
                        day.copy(imageUrl = photo)
                    }
                }.awaitAll()

                val finalItinerary = result.copy(days = daysWithPhotos)
                currentItinerary = finalItinerary

                // Save to vault
                val jsonString = Gson().toJson(finalItinerary)
                vaultDao.archiveToVault(
                    VaultEntry(
                        title = finalItinerary.title,
                        destination = userRequest,
                        jsonContent = jsonString
                    )
                )

                // Generate AI packing list
                generatePackingListInternal(
                    finalItinerary.primaryCountry ?: "Tanzania",
                    finalItinerary.parksVisited
                )

                // Get travel alerts
                val locations = finalItinerary.days.map { it.location }.distinct()
                val alerts = locations.mapNotNull { loc ->
                    IntelligenceLoader.getSpecificRule(userNationality, loc)?.smartTip
                }

                _uiState.value = ItineraryUiState.Success(finalItinerary, alerts)
                fetchOperators(finalItinerary.primaryCountry ?: "Tanzania", 5000.0, finalItinerary.parksVisited)

            } catch (e: Exception) {
                _uiState.value = ItineraryUiState.Error(e.localizedMessage ?: "AI Generation Failed.")
            }
        }
    }

    // Public method to generate packing list
    fun generatePackingList(country: String, parks: List<String>) {
        viewModelScope.launch {
            generatePackingListInternal(country, parks)
        }
    }

    private suspend fun generatePackingListInternal(country: String, parks: List<String>) {
        try {
            val list = RealAIPackingService.generatePackingList(
                country = country,
                parks = parks,
                duration = currentItinerary?.days?.size ?: 7
            )
            _packingList.value = list
        } catch (e: Exception) {
            val engineList = PackingIntelligenceEngine.generateList(country, parks)

            val mappedFallbackList = engineList.map { engineItem ->
                PackingItem(
                    name = engineItem.name,
                    reason = engineItem.reason,
                    category = "General",
                    essential = true
                )
            }

            _packingList.value = mappedFallbackList
        }
    }

    // Fetch operators — accepts forceRefresh so the UI can bypass cache
    private suspend fun fetchOperators(
        country: String,
        budget: Double,
        parks: List<String>,
        forceRefresh: Boolean = false
    ) {
        _operatorState.value = OperatorUiState.Loading
        try {
            val matches = operatorRepo.getMatches(country, budget, parks, forceRefresh)
            _operatorState.value = OperatorUiState.Success(matches)
        } catch (e: Exception) {
            _operatorState.value = OperatorUiState.Error("Operators unavailable")
        }
    }

    /** Public entry point for manual refresh — wire to a pull-to-refresh or refresh icon. */
    fun refreshOperators() {
        val itinerary = currentItinerary ?: return
        viewModelScope.launch {
            fetchOperators(
                itinerary.primaryCountry ?: "Tanzania",
                5000.0,
                itinerary.parksVisited,
                forceRefresh = true
            )
        }
    }

    // Save itinerary
    fun saveCurrentItinerary() {
        currentItinerary?.let { itinerary ->
            viewModelScope.launch {
                val jsonString = Gson().toJson(itinerary)
                savedItineraryDao.saveItinerary(
                    SavedItinerary(
                        title = itinerary.title,
                        destination = itinerary.parksVisited.joinToString(", "),
                        jsonContent = jsonString,
                        thumbnailUrl = itinerary.days.firstOrNull()?.imageUrl
                    )
                )
            }
        }
    }

    fun deleteSavedItinerary(itinerary: SavedItinerary) {
        viewModelScope.launch {
            savedItineraryDao.deleteItinerary(itinerary)
        }
    }

    fun loadSavedItinerary(savedItinerary: SavedItinerary) {
        try {
            val itinerary = Gson().fromJson(savedItinerary.jsonContent, SafariItinerary::class.java)
            currentItinerary = itinerary

            _uiState.value = ItineraryUiState.Success(itinerary, emptyList())

            viewModelScope.launch {
                generatePackingListInternal(
                    itinerary.primaryCountry ?: "Tanzania",
                    itinerary.parksVisited
                )
            }
        } catch (e: Exception) {
            _uiState.value = ItineraryUiState.Error("Failed to load saved itinerary")
        }
    }

    fun toggleFavorite(entry: VaultEntry) {
        viewModelScope.launch {
            val updated = entry.copy(isFavorite = !entry.isFavorite)
            vaultDao.updateEntry(updated)
        }
    }

    fun deleteEntry(entry: VaultEntry) {
        viewModelScope.launch {
            vaultDao.deleteFromVault(entry)
        }
    }

    // Booking / inquiry functions
    fun initiateBooking(operator: SafariOperator) {
        currentItinerary?.let { itinerary ->
            val bookingRequest = BookingRequest(
                operatorId = operator.id,
                operatorName = operator.name,
                itineraryTitle = itinerary.title,
                totalPrice = operator.basePrice,
                travelDate = "",
                numberOfTravelers = 2
            )
            _bookingState.value = BookingState.ReadyToBook(bookingRequest)
        }
    }

    /**
     * Submits an inquiry. Writes locally to Room via BookingDao (unchanged
     * behavior) AND, if BACKEND_BASE_URL is configured, also POSTs to the
     * real backend so the inquiry reaches a server your team can act on —
     * not just a value sitting in the user's local on-device database.
     *
     * If the network call fails, the local Room write still succeeds, so
     * the user still sees a success state and the inquiry isn't lost from
     * their perspective — but it means it hasn't reached your team yet.
     * A production version should add a retry/sync-outbox pattern (e.g.
     * WorkManager) for pending inquiries that failed to reach the backend;
     * that's flagged here rather than silently glossed over.
     */
    fun submitBooking(bookingRequest: BookingRequest) {
        viewModelScope.launch {
            _bookingState.value = BookingState.Processing
            try {
                val bookingEntity = BookingEntity(
                    operatorId = bookingRequest.operatorId,
                    operatorName = bookingRequest.operatorName,
                    itineraryTitle = bookingRequest.itineraryTitle,
                    totalPrice = bookingRequest.totalPrice,
                    travelerName = bookingRequest.travelerName,
                    travelerEmail = bookingRequest.travelerEmail,
                    travelerPhone = bookingRequest.travelerPhone,
                    travelDate = bookingRequest.travelDate,
                    numberOfTravelers = bookingRequest.numberOfTravelers,
                    specialRequests = bookingRequest.specialRequests
                )

                val insertedId = bookingDao.insertBooking(bookingEntity)

                // Best-effort remote sync — does not block or fail the
                // local success path if the backend is unreachable.
                InquiryApiClient.submitInquiry(bookingRequest, insertedId)

                AnalyticsTracker.logEvent(
                    "inquiry_submitted",
                    mapOf(
                        "operator_id" to bookingRequest.operatorId,
                        "num_travelers" to bookingRequest.numberOfTravelers.toString()
                    )
                )

                _bookingState.value = BookingState.Success(insertedId)
            } catch (e: Exception) {
                _bookingState.value = BookingState.Error(e.message ?: "Inquiry failed to submit")
            }
        }
    }

    fun resetToIdle() {
        _uiState.value = ItineraryUiState.Idle
        _bookingState.value = BookingState.Idle
    }

    fun resetBookingState() {
        _bookingState.value = BookingState.Idle
    }

    /**
     * Real share implementation. Builds a plain-text summary of the current
     * itinerary and emits it as a one-shot event. The Composable collects
     * this event and launches Android's native share sheet (ACTION_SEND) —
     * this is what actually invokes WhatsApp/Mail/etc., which a ViewModel
     * cannot do directly since it has no Context/Activity reference.
     */
    fun shareItinerary() {
        val itinerary = currentItinerary ?: return

        AnalyticsTracker.logEvent(
            "itinerary_share_initiated",
            mapOf(
                "destination" to (itinerary.primaryCountry ?: "unknown"),
                "day_count" to itinerary.days.size.toString()
            )
        )

        val summary = buildString {
            appendLine(itinerary.title)
            appendLine("Curated by Luxury Safari Architect")
            appendLine()
            appendLine("Estimated cost: ${itinerary.totalEstimatedCost}")
            itinerary.primaryCountry?.let { appendLine("Destination: $it") }
            if (itinerary.parksVisited.isNotEmpty()) {
                appendLine("Parks: ${itinerary.parksVisited.joinToString(", ")}")
            }
            appendLine()
            itinerary.days.forEach { day ->
                appendLine("Day ${day.day} — ${day.location}")
                day.activities.forEach { activity -> appendLine("  • $activity") }
                appendLine()
            }
            appendLine("Planned with Luxury Safari Architect")
        }

        viewModelScope.launch {
            _shareEvent.emit(
                ShareItineraryPayload(
                    subject = itinerary.title,
                    body = summary
                )
            )
        }
    }
}

/** Payload for the one-shot share event; kept simple (text only) for v1. */
data class ShareItineraryPayload(
    val subject: String,
    val body: String
)

sealed class BookingState {
    object Idle : BookingState()
    data class ReadyToBook(val request: BookingRequest) : BookingState()
    object Processing : BookingState()
    data class Success(val bookingId: Long) : BookingState()
    data class Error(val message: String) : BookingState()
}
