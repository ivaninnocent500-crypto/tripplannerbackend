package com.tta.africasafariguide

import android.util.Log
import com.google.ai.client.generativeai.GenerativeModel
import com.google.ai.client.generativeai.type.content
import com.google.ai.client.generativeai.type.generationConfig
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken

object RealAIPackingService {
    private const val TAG = "RealAIPackingService"

    private val model: GenerativeModel by lazy {
        GenerativeModel(
            modelName = "gemini-2.5-flash",
            apiKey = ApiKeyProvider.geminiApiKey, // FIX: was a hardcoded duplicate key
            generationConfig = generationConfig {
                responseMimeType = "application/json"
                temperature = 0.2f
            },
            systemInstruction = content {
                text("""
                    You are an expert African Safari packing consultant. 
                    Generate personalized packing lists based on:
                    1. Destination country and specific parks
                    2. Season and weather patterns
                    3. Activities planned
                    4. Duration of stay
                    
                    Return JSON array of packing items with this exact structure:
                    [
                      {
                        "name": "Item name",
                        "reason": "Why it's needed",
                        "category": "Clothing/Electronics/Medical/Gear/Documents",
                        "essential": true/false
                      }
                    ]
                    
                    Include 15-20 items covering:
                    - Climate-appropriate clothing
                    - Safari gear (binoculars, camera)
                    - Health & safety items
                    - Important documents
                    - Electronics
                    - Optional luxury items
                    
                    Make reasons specific to the destination and activities.
                """.trimIndent())
            }
        )
    }

    suspend fun generatePackingList(
        country: String,
        parks: List<String>,
        duration: Int = 7,
        month: String = "June"
    ): List<PackingItem> {
        return try {
            val prompt = """
                Create a packing list for a $duration-day safari in $country.
                Visiting these parks: ${parks.joinToString(", ")}
                Travel month: $month
                
                Consider:
                - Daytime temperatures
                - Morning/evening game drives
                - Photography opportunities
                - Health precautions for this region
            """.trimIndent()

            val response = model.generateContent(prompt)
            val rawText = response.text ?: throw Exception("AI returned empty response")

            val cleanJson = rawText.trim()
                .removePrefix("```json")
                .removePrefix("```")
                .removeSuffix("```")
                .trim()

            Log.d(TAG, "AI Packing List: $cleanJson")

            val type = object : TypeToken<List<PackingItem>>() {}.type
            Gson().fromJson<List<PackingItem>>(cleanJson, type) ?: emptyList()
        } catch (e: Exception) {
            Log.e(TAG, "Error generating packing list", e)
            getFallbackPackingList(country)
        }
    }

    private fun getFallbackPackingList(country: String): List<PackingItem> {
        return listOf(
            PackingItem("Lightweight Clothing", "Hot days and dusty roads", "Clothing", true),
            PackingItem("Warm Jacket", "Cold morning game drives", "Clothing", true),
            PackingItem("Binoculars", "Essential for wildlife viewing", "Gear", true),
            PackingItem("Camera with zoom lens", "Capture wildlife moments", "Electronics", true),
            PackingItem("Sunscreen SPF 50+", "Strong equatorial sun", "Health", true),
            PackingItem("Insect Repellent", "Mosquito protection", "Health", true),
            PackingItem("Reusable Water Bottle", "Stay hydrated", "Gear", true),
            PackingItem("Passport and Visas", "Required for entry to $country", "Documents", true),
            PackingItem("Yellow Fever Certificate", "Required for entry", "Documents", true),
            PackingItem("Power Bank", "Limited charging options", "Electronics", true),
            PackingItem("First Aid Kit", "Basic medical supplies", "Health", true),
            PackingItem("Wide-brimmed Hat", "Sun protection", "Clothing", true),
            PackingItem("Sunglasses", "Glare reduction", "Clothing", true),
            PackingItem("Comfortable Walking Shoes", "Nature walks", "Clothing", true),
            PackingItem("Flashlight/Headlamp", "Evening navigation", "Gear", true)
        )
    }
}
