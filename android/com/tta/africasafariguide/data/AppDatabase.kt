package com.tta.africasafariguide.data

import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import android.content.Context
import com.tta.africasafariguide.data.local.DestinationDao
import com.tta.africasafariguide.data.local.DestinationEntity
import com.tta.africasafariguide.data.local.VisaDao
import com.tta.africasafariguide.data.local.VisaRequirementEntity

@Database(
    entities = [
        VaultEntry::class,
        SavedItinerary::class,
        BookingEntity::class,
        DestinationEntity::class,
        VisaRequirementEntity::class
    ],
    version = 4,
    exportSchema = false
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun vaultDao(): VaultDao
    abstract fun savedItineraryDao(): SavedItineraryDao
    abstract fun bookingDao(): BookingDao

    abstract fun destinationDao(): DestinationDao

    abstract fun visaDao(): VisaDao

    companion object {
        @Volatile
        private var INSTANCE: AppDatabase? = null

        fun getInstance(context: Context): AppDatabase {
            return INSTANCE ?: synchronized(this) {
                val instance = Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "africa_safari_guide_db"
                ).fallbackToDestructiveMigration()
                    .build()
                INSTANCE = instance
                instance
            }
        }
    }
}
