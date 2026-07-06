package com.tta.africasafariguide

import android.util.Log
import com.google.ai.client.generativeai.GenerativeModel
import com.google.ai.client.generativeai.type.content
import com.google.ai.client.generativeai.type.generationConfig
import com.google.gson.Gson

object GeminiFlashService {
    private const val TAG = "GeminiFlashService"

    // FIX: Key now comes from the shared ApiKeyProvider (BuildConfig-backed),
    // not a literal string. See ApiKeyProvider.kt for details.
    private val model: GenerativeModel by lazy {
        GenerativeModel(
            modelName = "gemini-2.5-flash",
            apiKey = ApiKeyProvider.geminiApiKey,
            generationConfig = generationConfig {
                responseMimeType = "application/json"
            },
            systemInstruction = content {
                text("""
        You are an expert African Safari Guide. 
        Return travel plans in STRICT VALID JSON format only.
        
        CRITICAL SYNTAX RULES:
        1. Every object '{' MUST be closed with '}'.
        2. Every array '[' MUST be closed with ']'.
        3. Do NOT omit commas between items in the 'days' list.
        4. Ensure every closing brace for a 'day' object is followed by a comma if another day follows.
        
        Strictly follow this JSON structure exactly:
        {
          "title": "Trip Title",
          "totalEstimatedCost": "$2,500",
          "primaryCountry": "Tanzania",
          "countryId": 1,
          "parksVisited": ["Serengeti", "Ngorongoro"],
          "highlights": ["Great Migration", "Big Five"],
          "costBreakdown": {
            "transport": "4x4 Land Cruiser",
            "accommodation": "Luxury Tented Camp",
            "parkFees": "Included",
            "meals": "Full Board"
          },
          "travelAdvisory": "Safety and Visa tips",
          "days": [
            {
              "day": 1,
              "location": "Arusha",
              "activities": ["City Tour", "Market Visit"],
              "latitude": -3.37,
              "longitude": 36.68,
              "weatherIcon": "Sunny",
              "temperature": "25°C"
            }
          ]
        }
        
        Data Rules:
        - primaryCountry (String) and parksVisited (List of Strings) are MANDATORY.
        - countryId must be an Integer.
        - latitude and longitude must be Double numbers.
        - weatherIcon must be 'Sunny', 'Rainy', or 'Cloudy'.
    """.trimIndent())
            }
        )
    }

    suspend fun generateSafari(userInput: String): SafariItinerary {
        return try {
            val response = model.generateContent(userInput)
            val rawText = response.text ?: throw Exception("AI returned empty response")

            val cleanJson = rawText.trim()
                .removePrefix("```json")
                .removePrefix("```")
                .removeSuffix("```")
                .trim()

            Log.d(TAG, "Cleaned Gemini Response: $cleanJson")

            Gson().fromJson(cleanJson, SafariItinerary::class.java)
        } catch (e: Exception) {
            Log.e(TAG, "Error generating or parsing safari", e)
            throw e
        }
    }
}
