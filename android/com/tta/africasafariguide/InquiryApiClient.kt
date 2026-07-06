package com.tta.africasafariguide

import android.util.Log
import com.google.gson.Gson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.IOException
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL

/**
 * Sends inquiry submissions to the real backend (see /backend in this
 * delivery). This is the client-side half of turning "CONFIRM BOOKING" from
 * a purely local Room write into something that actually reaches a server
 * your team can act on.
 *
 * BACKEND_BASE_URL is a placeholder — replace it once you deploy the
 * backend (see backend/README.md for deploy steps to Render/Railway/Fly.io).
 * Until replaced, submitInquiry() will fail fast and log a warning; the
 * caller (ItineraryViewModel.submitBooking) treats that as non-fatal, since
 * the local Room write already succeeded.
 */
object InquiryApiClient {
    private const val TAG = "InquiryApiClient"

    // ⚠️ PLACEHOLDER — replace with your deployed backend URL, e.g.:
    // "https://africa-safari-guide-backend.onrender.com"
    private const val BACKEND_BASE_URL = "https://REPLACE_ME_WITH_YOUR_BACKEND_URL"

    private data class InquiryPayload(
        val localBookingId: Long,
        val operatorId: String,
        val operatorName: String,
        val itineraryTitle: String,
        val totalPrice: Int,
        val travelerName: String,
        val travelerEmail: String,
        val travelerPhone: String,
        val travelDate: String,
        val numberOfTravelers: Int,
        val specialRequests: String
    )

    /**
     * Fire-and-forget POST to the backend's /api/inquiries endpoint.
     * Returns true on HTTP 200/201, false on any failure — never throws,
     * since a failed remote sync should not fail the user-visible flow
     * (the local Room record is the source of truth for the UI regardless).
     */
    suspend fun submitInquiry(request: BookingRequest, localBookingId: Long): Boolean =
        withContext(Dispatchers.IO) {
            if (BACKEND_BASE_URL.contains("REPLACE_ME")) {
                Log.w(TAG, "Backend URL not configured — skipping remote sync. " +
                    "Inquiry is saved locally only. See InquiryApiClient.BACKEND_BASE_URL.")
                return@withContext false
            }

            try {
                val payload = InquiryPayload(
                    localBookingId = localBookingId,
                    operatorId = request.operatorId,
                    operatorName = request.operatorName,
                    itineraryTitle = request.itineraryTitle,
                    totalPrice = request.totalPrice,
                    travelerName = request.travelerName,
                    travelerEmail = request.travelerEmail,
                    travelerPhone = request.travelerPhone,
                    travelDate = request.travelDate,
                    numberOfTravelers = request.numberOfTravelers,
                    specialRequests = request.specialRequests
                )

                val json = Gson().toJson(payload)
                val url = URL("$BACKEND_BASE_URL/api/inquiries")
                val connection = url.openConnection() as HttpURLConnection

                connection.requestMethod = "POST"
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.doOutput = true
                connection.connectTimeout = 8000
                connection.readTimeout = 8000

                OutputStreamWriter(connection.outputStream, "UTF-8").use { writer ->
                    writer.write(json)
                    writer.flush()
                }

                val code = connection.responseCode
                if (code == HttpURLConnection.HTTP_OK || code == HttpURLConnection.HTTP_CREATED) {
                    Log.d(TAG, "Inquiry synced to backend successfully")
                    true
                } else {
                    Log.e(TAG, "Backend rejected inquiry: HTTP $code")
                    false
                }
            } catch (e: IOException) {
                Log.e(TAG, "Network error syncing inquiry to backend", e)
                false
            } catch (e: Exception) {
                Log.e(TAG, "Unexpected error syncing inquiry", e)
                false
            }
        }
}
