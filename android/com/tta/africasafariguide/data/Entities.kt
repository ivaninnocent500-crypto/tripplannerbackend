package com.tta.africasafariguide.data

import androidx.room.Entity
import androidx.room.PrimaryKey
import java.util.Date

@Entity(tableName = "vault_entries")
data class VaultEntry(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val title: String,
    val destination: String,
    val jsonContent: String,
    val timestamp: Long = System.currentTimeMillis(),
    val isFavorite: Boolean = false
)

@Entity(tableName = "saved_itineraries")
data class SavedItinerary(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val title: String,
    val destination: String,
    val jsonContent: String,
    val dateSaved: Long = System.currentTimeMillis(),
    val thumbnailUrl: String? = null
)

@Entity(tableName = "booking_requests")
data class BookingEntity(
    @PrimaryKey(autoGenerate = true)
    val id: Long = 0,
    val operatorId: String,
    val operatorName: String,
    val itineraryTitle: String,
    val totalPrice: Int,
    val travelerName: String,
    val travelerEmail: String,
    val travelerPhone: String,
    val travelDate: String,
    val numberOfTravelers: Int,
    val specialRequests: String,
    val bookingReference: String? = null,
    val status: String = "PENDING",
    val timestamp: Long = System.currentTimeMillis()
)
