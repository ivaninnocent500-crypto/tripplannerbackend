package com.tta.africasafariguide

import android.content.Context
import android.content.Intent
import android.widget.Toast
import androidx.compose.animation.*
import androidx.compose.animation.core.InfiniteTransition
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.BookmarkBorder
import androidx.compose.material.icons.outlined.Delete
import androidx.compose.material.icons.outlined.Share
import androidx.compose.ui.graphics.Brush
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.navigation.NavController
import coil.compose.AsyncImage
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.TextFieldDefaults
import java.util.*
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PremiumItineraryScreen(
    viewModel: ItineraryViewModel,
    navController: NavController,
    userNationality: String,
    onBack: () -> Unit,
    onNavigateToCompare: () -> Unit,
    onNavigateToVault: () -> Unit
) {
    val state by viewModel.uiState.collectAsState()
    val operatorState by viewModel.operatorState.collectAsState()
    val selectedOps by viewModel.selectedOperators.collectAsState()
    val packingList by viewModel.packingList.collectAsState()
    val inquiryState by viewModel.bookingState.collectAsState()
    val scope = rememberCoroutineScope()
    val context = LocalContext.current

    var userRequest by remember { mutableStateOf("") }
    var showBookingDialog by remember { mutableStateOf(false) }
    var selectedOperatorForBooking by remember { mutableStateOf<SafariOperator?>(null) }

    var showScrollIndicator by remember { mutableStateOf(true) }

    val deepEvergreen = Color(0xFF1B2623)
    val warmCanvas = Color(0xFFF5F2ED)
    val burnishedGold = Color(0xFFC5A059)

    LaunchedEffect(inquiryState) {
        when (inquiryState) {
            is BookingState.Success -> {
                Toast.makeText(
                    context,
                    "Inquiry sent! Reference #${(inquiryState as BookingState.Success).bookingId} — " +
                        "${selectedOperatorForBooking?.name ?: "the operator"} will confirm within 24–48h.",
                    Toast.LENGTH_LONG
                ).show()
                viewModel.resetBookingState()
                showBookingDialog = false
            }
            is BookingState.Error -> {
                Toast.makeText(context, (inquiryState as BookingState.Error).message, Toast.LENGTH_LONG).show()
                viewModel.resetBookingState()
            }
            else -> {}
        }
    }

    LaunchedEffect(Unit) {
        viewModel.shareEvent.collect { payload ->
            val sendIntent = Intent().apply {
                action = Intent.ACTION_SEND
                putExtra(Intent.EXTRA_SUBJECT, payload.subject)
                putExtra(Intent.EXTRA_TEXT, payload.body)
                type = "text/plain"
            }
            val shareIntent = Intent.createChooser(sendIntent, "Share your safari itinerary")
            context.startActivity(shareIntent)
        }
    }

    Scaffold(
        containerColor = deepEvergreen,
        topBar = {
            TopAppBar(
                colors = TopAppBarDefaults.topAppBarColors(containerColor = Color.Transparent),
                title = {
                    Text(
                        "LUXURY SAFARI ARCHITECT",
                        color = warmCanvas,
                        fontWeight = FontWeight.Light,
                        fontSize = 14.sp,
                        letterSpacing = 2.sp
                    )
                },
                actions = {
                    IconButton(onClick = onNavigateToVault) {
                        Icon(
                            Icons.Outlined.BookmarkBorder,
                            "Saved",
                            tint = burnishedGold,
                            modifier = Modifier.size(20.dp)
                        )
                    }

                    if (state is ItineraryUiState.Success) {
                        IconButton(
                            onClick = {
                                viewModel.saveCurrentItinerary()
                                Toast.makeText(context, "Itinerary saved!", Toast.LENGTH_SHORT).show()
                            }
                        ) {
                            Icon(
                                Icons.Default.Save,
                                "Save",
                                tint = burnishedGold,
                                modifier = Modifier.size(20.dp)
                            )
                        }

                        IconButton(
                            onClick = {
                                viewModel.shareItinerary()
                            }
                        ) {
                            Icon(
                                Icons.Outlined.Share,
                                "Share",
                                tint = burnishedGold,
                                modifier = Modifier.size(20.dp)
                            )
                        }
                    }
                },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            Icons.Default.ArrowBack,
                            null,
                            tint = warmCanvas
                        )
                    }
                }
            )
        },
        floatingActionButton = {
            if (selectedOps.size == 2) {
                ExtendedFloatingActionButton(
                    onClick = onNavigateToCompare,
                    containerColor = burnishedGold,
                    contentColor = deepEvergreen,
                    shape = RoundedCornerShape(24.dp)
                ) {
                    Icon(Icons.Default.CompareArrows, null, modifier = Modifier.size(16.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("COMPARE (2)", fontWeight = FontWeight.Medium, fontSize = 12.sp)
                }
            }
        }
    ) { padding ->
        Box(modifier = Modifier.padding(padding)) {
            AnimatedContent(
                targetState = state,
                label = "ContentFade"
            ) { uiState ->
                when (uiState) {
                    is ItineraryUiState.Success -> {
                        val itinerary = uiState.itinerary
                        LazyColumn(
                            modifier = Modifier.fillMaxSize(),
                            contentPadding = PaddingValues(bottom = 24.dp),
                            verticalArrangement = Arrangement.spacedBy(24.dp)
                        ) {
                            item {
                                ImmersiveHeader(
                                    imageUrl = "https://example.com/serengeti-sunset.jpg",
                                    title = itinerary.title,
                                    warmCanvas = warmCanvas,
                                    onScrollIndicatorChange = { showScrollIndicator = it }
                                )
                            }

                            item {
                                CostHorizontalRibbon(
                                    breakdown = itinerary.costBreakdown,
                                    burnishedGold = burnishedGold,
                                    warmCanvas = warmCanvas,
                                    deepEvergreen = deepEvergreen
                                )
                            }

                            item {
                                AnimatedScrollIndicator(
                                    showIndicator = showScrollIndicator,
                                    burnishedGold = burnishedGold
                                )
                            }

                            itemsIndexed(itinerary.days) { index, day ->
                                if (index == 0) {
                                    DayOnePeekCard(
                                        day = day,
                                        burnishedGold = burnishedGold,
                                        warmCanvas = warmCanvas,
                                        deepEvergreen = deepEvergreen
                                    )
                                } else {
                                    TimelineDayItem(
                                        day = day,
                                        isLast = index == itinerary.days.size - 1,
                                        burnishedGold = burnishedGold,
                                        warmCanvas = warmCanvas,
                                        deepEvergreen = deepEvergreen
                                    )
                                }
                            }

                            item {
                                AIPackingListSection(
                                    packingList = packingList,
                                    onRefresh = {
                                        viewModel.generatePackingList(
                                            itinerary.primaryCountry ?: "Tanzania",
                                            itinerary.parksVisited
                                        )
                                    },
                                    burnishedGold = burnishedGold,
                                    warmCanvas = warmCanvas,
                                    deepEvergreen = deepEvergreen
                                )
                            }

                            item {
                                OperatorMarketplaceSection(
                                    operatorState = operatorState,
                                    selectedOperators = selectedOps,
                                    onToggleCompare = { operator ->
                                        viewModel.toggleOperatorSelection(operator)
                                    },
                                    onBookNow = { operator ->
                                        selectedOperatorForBooking = operator
                                        showBookingDialog = true
                                    },
                                    onRefresh = {
                                        viewModel.refreshOperators()
                                    },
                                    burnishedGold = burnishedGold,
                                    warmCanvas = warmCanvas,
                                    deepEvergreen = deepEvergreen
                                )
                            }

                            item {
                                TravelAdvisoryCard(
                                    advisory = itinerary.travelAdvisory,
                                    burnishedGold = burnishedGold,
                                    warmCanvas = warmCanvas
                                )
                            }
                        }
                    }

                    is ItineraryUiState.Loading -> {
                        LoadingView(burnishedGold, warmCanvas)
                    }

                    is ItineraryUiState.Error -> {
                        ErrorView(uiState.message, onRetry = { viewModel.resetToIdle() }, burnishedGold, warmCanvas)
                    }

                    else -> {
                        WelcomeGuide(
                            userRequest = userRequest,
                            onValueChange = { userRequest = it },
                            onGenerate = {
                                if (userRequest.isNotBlank()) {
                                    viewModel.generateTrip(userRequest, userNationality)
                                }
                            },
                            burnishedGold = burnishedGold,
                            warmCanvas = warmCanvas,
                            deepEvergreen = deepEvergreen
                        )
                    }
                }
            }

            if (showBookingDialog && selectedOperatorForBooking != null) {
                InquiryDialog(
                    operator = selectedOperatorForBooking!!,
                    itinerary = (state as? ItineraryUiState.Success)?.itinerary,
                    onDismiss = {
                        showBookingDialog = false
                        selectedOperatorForBooking = null
                    },
                    onConfirm = { bookingRequest ->
                        viewModel.submitBooking(bookingRequest)
                    },
                    burnishedGold = burnishedGold,
                    warmCanvas = warmCanvas,
                    deepEvergreen = deepEvergreen
                )
            }
        }
    }
}

@Composable
fun ImmersiveHeader(
    imageUrl: String,
    title: String,
    warmCanvas: Color,
    onScrollIndicatorChange: (Boolean) -> Unit
) {
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(300.dp)
    ) {
        AsyncImage(
            model = imageUrl,
            contentDescription = "Serengeti Sunset",
            modifier = Modifier
                .fillMaxSize()
                .clip(RoundedCornerShape(bottomStart = 24.dp, bottomEnd = 24.dp)),
            contentScale = ContentScale.Crop
        )

        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(
                    brush = Brush.verticalGradient(
                        colors = listOf(
                            Color.Transparent,
                            Color(0xFF1B2623).copy(alpha = 0.8f)
                        ),
                        startY = 200f
                    )
                )
                .clip(RoundedCornerShape(bottomStart = 24.dp, bottomEnd = 24.dp))
        )

        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.Bottom
        ) {
            Text(
                title.uppercase(),
                color = warmCanvas,
                fontSize = 32.sp,
                fontWeight = FontWeight.Light,
                letterSpacing = 4.sp,
                lineHeight = 40.sp,
                maxLines = 2
            )

            Spacer(modifier = Modifier.height(8.dp))

            Text(
                "CURATED BY LUXURY SAFARI ARCHITECT",
                color = warmCanvas.copy(alpha = 0.7f),
                fontSize = 10.sp,
                fontWeight = FontWeight.Medium,
                letterSpacing = 2.sp
            )
        }
    }
}

@Composable
fun CostHorizontalRibbon(
    breakdown: CostBreakdown?,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Column {
        Text(
            "YOUR INVESTMENT".uppercase(),
            color = warmCanvas.copy(alpha = 0.5f),
            fontSize = 12.sp,
            fontWeight = FontWeight.Medium,
            letterSpacing = 2.sp,
            modifier = Modifier.padding(horizontal = 24.dp)
        )

        Spacer(modifier = Modifier.height(16.dp))

        LazyRow(
            horizontalArrangement = Arrangement.spacedBy(16.dp),
            contentPadding = PaddingValues(start = 24.dp, end = 16.dp),
            modifier = Modifier.fillMaxWidth()
        ) {
            if (breakdown != null) {
                items(
                    listOf(
                        Triple("TRANSPORT", breakdown.transport, Icons.Default.Flight),
                        Triple("LODGING", breakdown.accommodation, Icons.Default.Hotel),
                        Triple("PARK FEES", breakdown.parkFees, Icons.Default.LocalActivity),
                        Triple("MEALS", breakdown.meals, Icons.Default.Restaurant)
                    )
                ) { item ->
                    CostRibbonCard(
                        label = item.first,
                        value = item.second,
                        icon = item.third,
                        burnishedGold = burnishedGold,
                        warmCanvas = warmCanvas,
                        deepEvergreen = deepEvergreen
                    )
                }
            }
        }
    }
}

@Composable
fun CostRibbonCard(
    label: String,
    value: String,
    icon: ImageVector,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Card(
        modifier = Modifier
            .width(140.dp)
            .height(100.dp),
        colors = CardDefaults.cardColors(
            containerColor = deepEvergreen.copy(alpha = 0.6f)
        ),
        shape = RoundedCornerShape(24.dp),
        elevation = CardDefaults.cardElevation(
            defaultElevation = 8.dp
        ),
        border = BorderStroke(1.dp, burnishedGold.copy(alpha = 0.3f))
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(16.dp),
            verticalArrangement = Arrangement.SpaceBetween
        ) {
            Icon(
                icon,
                contentDescription = null,
                tint = burnishedGold,
                modifier = Modifier.size(20.dp)
            )

            Column {
                Text(
                    value,
                    color = warmCanvas,
                    fontSize = 20.sp,
                    fontWeight = FontWeight.Light,
                    letterSpacing = 1.sp
                )
                Text(
                    label,
                    color = warmCanvas.copy(alpha = 0.5f),
                    fontSize = 10.sp,
                    fontWeight = FontWeight.Medium,
                    letterSpacing = 1.sp
                )
            }
        }
    }
}

@Composable
fun AnimatedScrollIndicator(
    showIndicator: Boolean,
    burnishedGold: Color
) {
    AnimatedVisibility(
        visible = showIndicator,
        enter = fadeIn() + expandVertically(),
        exit = fadeOut() + shrinkVertically()
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 8.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            val infiniteTransition = rememberInfiniteTransition()
            val alpha by infiniteTransition.animateFloat(
                initialValue = 0.3f,
                targetValue = 1f,
                animationSpec = infiniteRepeatable(
                    animation = tween(1000),
                    repeatMode = RepeatMode.Reverse
                )
            )

            Icon(
                Icons.Default.ExpandMore,
                contentDescription = "Scroll Down",
                tint = burnishedGold.copy(alpha = alpha),
                modifier = Modifier.size(32.dp)
            )

            Text(
                "SCROLL FOR DAY 1",
                color = burnishedGold.copy(alpha = alpha),
                fontSize = 10.sp,
                fontWeight = FontWeight.Medium,
                letterSpacing = 2.sp,
                modifier = Modifier.padding(top = 4.dp)
            )
        }
    }
}

@Composable
fun DayOnePeekCard(
    day: DailyPlan,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 24.dp),
        colors = CardDefaults.cardColors(
            containerColor = deepEvergreen.copy(alpha = 0.8f)
        ),
        shape = RoundedCornerShape(24.dp),
        elevation = CardDefaults.cardElevation(
            defaultElevation = 16.dp
        ),
        border = BorderStroke(1.dp, burnishedGold.copy(alpha = 0.2f))
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(24.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    "DAY 1 • ${day.location.uppercase()}",
                    color = burnishedGold,
                    fontWeight = FontWeight.Medium,
                    fontSize = 14.sp,
                    letterSpacing = 2.sp
                )

                Surface(
                    color = burnishedGold.copy(alpha = 0.1f),
                    shape = RoundedCornerShape(16.dp)
                ) {
                    Row(
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Icon(
                            imageVector = when (day.weatherIcon) {
                                "Sunny" -> Icons.Default.WbSunny
                                "Rainy" -> Icons.Default.BeachAccess
                                else -> Icons.Default.Cloud
                            },
                            contentDescription = null,
                            tint = burnishedGold,
                            modifier = Modifier.size(14.dp)
                        )
                        Text(
                            day.temperature,
                            color = warmCanvas,
                            fontSize = 12.sp,
                            fontWeight = FontWeight.Medium,
                            modifier = Modifier.padding(start = 4.dp)
                        )
                    }
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            day.activities.take(2).forEach { activity ->
                Row(
                    verticalAlignment = Alignment.Top,
                    modifier = Modifier.padding(vertical = 4.dp)
                ) {
                    Text(
                        "•",
                        color = burnishedGold,
                        fontSize = 16.sp,
                        modifier = Modifier.padding(end = 8.dp)
                    )
                    Text(
                        activity,
                        color = warmCanvas.copy(alpha = 0.8f),
                        fontSize = 14.sp,
                        fontWeight = FontWeight.Light,
                        lineHeight = 20.sp
                    )
                }
            }

            if (day.activities.size > 2) {
                Text(
                    "+${day.activities.size - 2} more activities",
                    color = burnishedGold,
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium,
                    modifier = Modifier.padding(top = 8.dp)
                )
            }
        }
    }
}

@Composable
fun TimelineDayItem(
    day: DailyPlan,
    isLast: Boolean,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 24.dp)
            .height(IntrinsicSize.Min)
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier.width(30.dp)
        ) {
            Box(
                modifier = Modifier
                    .size(12.dp)
                    .clip(CircleShape)
                    .background(burnishedGold)
            )
            if (!isLast) {
                Box(
                    modifier = Modifier
                        .fillMaxHeight()
                        .width(1.dp)
                        .background(burnishedGold.copy(alpha = 0.2f))
                )
            }
        }

        Card(
            modifier = Modifier
                .weight(1f)
                .padding(start = 12.dp, bottom = if (isLast) 0.dp else 24.dp),
            colors = CardDefaults.cardColors(
                containerColor = deepEvergreen.copy(alpha = 0.4f)
            ),
            shape = RoundedCornerShape(16.dp),
            border = BorderStroke(1.dp, burnishedGold.copy(alpha = 0.1f))
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(16.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        "DAY ${day.day} • ${day.location.uppercase()}",
                        color = burnishedGold,
                        fontWeight = FontWeight.Medium,
                        fontSize = 12.sp,
                        letterSpacing = 1.sp
                    )
                }

                Spacer(modifier = Modifier.height(12.dp))
                day.activities.forEach { activity ->
                    Row(
                        verticalAlignment = Alignment.Top,
                        modifier = Modifier.padding(vertical = 4.dp)
                    ) {
                        Text(
                            "•",
                            color = burnishedGold,
                            fontSize = 14.sp,
                            modifier = Modifier.padding(end = 8.dp)
                        )
                        Text(
                            activity,
                            color = warmCanvas.copy(alpha = 0.7f),
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Light,
                            lineHeight = 18.sp
                        )
                    }
                }
            }
        }
    }
}

@Composable
fun AIPackingListSection(
    packingList: List<PackingItem>,
    onRefresh: () -> Unit,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Column(
        modifier = Modifier.padding(horizontal = 24.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                "CURATED PACKING LIST".uppercase(),
                color = warmCanvas.copy(alpha = 0.5f),
                fontWeight = FontWeight.Medium,
                fontSize = 12.sp,
                letterSpacing = 2.sp
            )

            IconButton(onClick = onRefresh) {
                Icon(
                    Icons.Default.Refresh,
                    contentDescription = "Refresh",
                    tint = burnishedGold,
                    modifier = Modifier.size(18.dp)
                )
            }
        }

        Spacer(Modifier.height(16.dp))

        if (packingList.isEmpty()) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(100.dp),
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator(color = burnishedGold)
            }
        } else {
            LazyRow(
                horizontalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                items(packingList) { item ->
                    PackingItemCard(
                        item = item,
                        burnishedGold = burnishedGold,
                        warmCanvas = warmCanvas,
                        deepEvergreen = deepEvergreen
                    )
                }
            }
        }
    }
}

@Composable
fun PackingItemCard(
    item: PackingItem,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Card(
        modifier = Modifier
            .width(200.dp),
        colors = CardDefaults.cardColors(
            containerColor = deepEvergreen.copy(alpha = 0.6f)
        ),
        shape = RoundedCornerShape(24.dp),
        border = BorderStroke(1.dp, burnishedGold.copy(alpha = 0.2f)),
        elevation = CardDefaults.cardElevation(4.dp)
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(20.dp)
        ) {
            Surface(
                color = burnishedGold.copy(alpha = 0.15f),
                shape = RoundedCornerShape(8.dp),
                border = BorderStroke(1.dp, burnishedGold.copy(alpha = 0.3f))
            ) {
                Text(
                    item.category.uppercase(),
                    color = burnishedGold,
                    fontSize = 8.sp,
                    fontWeight = FontWeight.Medium,
                    letterSpacing = 1.sp,
                    modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                )
            }

            Spacer(Modifier.height(12.dp))

            Text(
                item.name,
                color = warmCanvas,
                fontWeight = FontWeight.Medium,
                fontSize = 16.sp,
                letterSpacing = 0.5.sp
            )

            Text(
                item.reason,
                color = warmCanvas.copy(alpha = 0.5f),
                fontSize = 11.sp,
                lineHeight = 14.sp,
                maxLines = 3,
                modifier = Modifier.padding(top = 8.dp)
            )

            if (item.essential) {
                Spacer(Modifier.height(12.dp))
                Row(
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Icon(
                        Icons.Default.PriorityHigh,
                        contentDescription = null,
                        tint = burnishedGold,
                        modifier = Modifier.size(12.dp)
                    )
                    Text(
                        " ESSENTIAL",
                        color = burnishedGold,
                        fontSize = 8.sp,
                        fontWeight = FontWeight.Bold,
                        letterSpacing = 1.sp
                    )
                }
            }
        }
    }
}

@Composable
fun TravelAdvisoryCard(
    advisory: String,
    burnishedGold: Color,
    warmCanvas: Color
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 24.dp),
        colors = CardDefaults.cardColors(
            containerColor = burnishedGold.copy(alpha = 0.1f)
        ),
        shape = RoundedCornerShape(24.dp),
        border = BorderStroke(1.dp, burnishedGold.copy(alpha = 0.3f))
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(20.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(
                Icons.Default.Info,
                contentDescription = null,
                tint = burnishedGold,
                modifier = Modifier.size(20.dp)
            )
            Spacer(Modifier.width(12.dp))
            Text(
                advisory,
                color = warmCanvas,
                fontSize = 13.sp,
                fontWeight = FontWeight.Light,
                lineHeight = 18.sp,
                modifier = Modifier.weight(1f)
            )
        }
    }
}

@Composable
fun OperatorMarketplaceSection(
    operatorState: OperatorUiState,
    selectedOperators: List<SafariOperator>,
    onToggleCompare: (SafariOperator) -> Unit,
    onBookNow: (SafariOperator) -> Unit,
    onRefresh: () -> Unit,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 24.dp)
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text(
                    "FEATURED OPERATORS".uppercase(),
                    color = warmCanvas.copy(alpha = 0.5f),
                    fontWeight = FontWeight.Medium,
                    fontSize = 12.sp,
                    letterSpacing = 2.sp
                )

                Text(
                    "Select up to 2 to compare",
                    color = warmCanvas.copy(alpha = 0.3f),
                    fontSize = 10.sp,
                    letterSpacing = 1.sp,
                    modifier = Modifier.padding(top = 4.dp)
                )
            }

            IconButton(onClick = onRefresh) {
                Icon(
                    Icons.Default.Refresh,
                    contentDescription = "Refresh operators",
                    tint = burnishedGold,
                    modifier = Modifier.size(18.dp)
                )
            }
        }

        Spacer(modifier = Modifier.height(16.dp))

        when (operatorState) {
            is OperatorUiState.Loading -> {
                Box(
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(200.dp),
                    contentAlignment = Alignment.Center
                ) {
                    CircularProgressIndicator(
                        color = burnishedGold,
                        strokeWidth = 2.dp
                    )
                }
            }
            is OperatorUiState.Success -> {
                Column(
                    verticalArrangement = Arrangement.spacedBy(16.dp)
                ) {
                    operatorState.operators.forEach { operator ->
                        val isSelected = selectedOperators.any { it.id == operator.id }

                        OperatorCard(
                            operator = operator,
                            isSelected = isSelected,
                            onCompareClick = { onToggleCompare(operator) },
                            onBookClick = { onBookNow(operator) },
                            burnishedGold = burnishedGold,
                            warmCanvas = warmCanvas,
                            deepEvergreen = deepEvergreen
                        )
                    }
                }
            }
            is OperatorUiState.Error -> {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(
                        containerColor = deepEvergreen.copy(alpha = 0.6f)
                    ),
                    shape = RoundedCornerShape(24.dp),
                    border = BorderStroke(1.dp, burnishedGold.copy(alpha = 0.2f))
                ) {
                    Text(
                        "Unable to load operators",
                        color = warmCanvas.copy(alpha = 0.7f),
                        fontSize = 14.sp,
                        modifier = Modifier.padding(24.dp)
                    )
                }
            }
        }
    }
}

@Composable
fun OperatorCard(
    operator: SafariOperator,
    isSelected: Boolean,
    onCompareClick: () -> Unit,
    onBookClick: () -> Unit,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = deepEvergreen.copy(alpha = 0.6f)
        ),
        shape = RoundedCornerShape(24.dp),
        elevation = CardDefaults.cardElevation(
            defaultElevation = 8.dp
        ),
        border = BorderStroke(
            width = if (isSelected) 2.dp else 1.dp,
            color = if (isSelected) burnishedGold else burnishedGold.copy(alpha = 0.2f)
        )
    ) {
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(20.dp)
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    operator.name.uppercase(),
                    color = warmCanvas,
                    fontWeight = FontWeight.Medium,
                    fontSize = 16.sp,
                    letterSpacing = 1.sp
                )

                if (isSelected) {
                    Surface(
                        color = burnishedGold,
                        shape = RoundedCornerShape(8.dp)
                    ) {
                        Text(
                            "SELECTED",
                            color = deepEvergreen,
                            fontSize = 8.sp,
                            fontWeight = FontWeight.Medium,
                            letterSpacing = 1.sp,
                            modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                        )
                    }
                }
            }

            Row(
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.padding(top = 8.dp)
            ) {
                repeat(5) { index ->
                    Icon(
                        Icons.Default.Star,
                        contentDescription = null,
                        tint = if (index < operator.rating.toInt()) burnishedGold else warmCanvas.copy(alpha = 0.2f),
                        modifier = Modifier.size(14.dp)
                    )
                }
                Text(
                    " ${operator.rating} (${operator.reviews} reviews)",
                    color = warmCanvas.copy(alpha = 0.5f),
                    fontSize = 11.sp
                )
            }

            Row(
                modifier = Modifier.padding(top = 8.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    "${operator.matchScore}% MATCH",
                    color = burnishedGold,
                    fontWeight = FontWeight.Medium,
                    fontSize = 12.sp,
                    letterSpacing = 1.sp
                )
            }

            Spacer(modifier = Modifier.height(16.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceEvenly
            ) {
                OperatorDetailItem(
                    label = "PRICE",
                    value = "$${operator.basePrice}/pax",
                    burnishedGold = burnishedGold,
                    warmCanvas = warmCanvas
                )
                OperatorDetailItem(
                    label = "VEHICLE",
                    value = operator.vehicleType,
                    burnishedGold = burnishedGold,
                    warmCanvas = warmCanvas
                )
                OperatorDetailItem(
                    label = "TRANSFER",
                    value = operator.airportTransfer,
                    burnishedGold = burnishedGold,
                    warmCanvas = warmCanvas
                )
            }

            Spacer(modifier = Modifier.height(16.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                OutlinedButton(
                    onClick = onCompareClick,
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.outlinedButtonColors(
                        contentColor = if (isSelected) burnishedGold else warmCanvas
                    ),
                    border = BorderStroke(
                        1.dp,
                        if (isSelected) burnishedGold else warmCanvas.copy(alpha = 0.3f)
                    ),
                    shape = RoundedCornerShape(16.dp)
                ) {
                    Text(
                        if (isSelected) "REMOVE" else "COMPARE",
                        fontSize = 11.sp,
                        fontWeight = FontWeight.Medium,
                        letterSpacing = 1.sp
                    )
                }

                Button(
                    onClick = onBookClick,
                    modifier = Modifier.weight(1f),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = burnishedGold
                    ),
                    shape = RoundedCornerShape(16.dp)
                ) {
                    Text(
                        "GET QUOTE",
                        color = deepEvergreen,
                        fontSize = 11.sp,
                        fontWeight = FontWeight.Medium,
                        letterSpacing = 1.sp
                    )
                }
            }
        }
    }
}

@Composable
fun OperatorDetailItem(
    label: String,
    value: String,
    burnishedGold: Color,
    warmCanvas: Color
) {
    Column(
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            label,
            color = warmCanvas.copy(alpha = 0.3f),
            fontSize = 9.sp,
            letterSpacing = 1.sp
        )
        Text(
            value,
            color = warmCanvas,
            fontSize = 12.sp,
            fontWeight = FontWeight.Medium,
            modifier = Modifier.padding(top = 4.dp)
        )
    }
}

@Composable
fun LoadingView(burnishedGold: Color, warmCanvas: Color) {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            CircularProgressIndicator(
                color = burnishedGold,
                strokeWidth = 2.dp
            )
            Spacer(modifier = Modifier.height(16.dp))
            Text(
                "CRAFTING YOUR LUXURY SAFARI",
                color = warmCanvas.copy(alpha = 0.7f),
                fontSize = 12.sp,
                fontWeight = FontWeight.Light,
                letterSpacing = 2.sp
            )
        }
    }
}

@Composable
fun ErrorView(
    message: String,
    onRetry: () -> Unit,
    burnishedGold: Color,
    warmCanvas: Color
) {
    Box(
        modifier = Modifier.fillMaxSize(),
        contentAlignment = Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            modifier = Modifier.padding(32.dp)
        ) {
            Icon(
                Icons.Default.Error,
                contentDescription = null,
                tint = burnishedGold,
                modifier = Modifier.size(48.dp)
            )
            Spacer(modifier = Modifier.height(16.dp))
            Text(
                message,
                color = warmCanvas,
                fontSize = 14.sp,
                fontWeight = FontWeight.Light,
                textAlign = TextAlign.Center
            )
            Spacer(modifier = Modifier.height(24.dp))
            Button(
                onClick = onRetry,
                colors = ButtonDefaults.buttonColors(
                    containerColor = burnishedGold
                ),
                shape = RoundedCornerShape(24.dp)
            ) {
                Text(
                    "RETRY",
                    color = Color(0xFF1B2623),
                    fontWeight = FontWeight.Medium,
                    fontSize = 12.sp,
                    letterSpacing = 2.sp,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp)
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun WelcomeGuide(
    userRequest: String,
    onValueChange: (String) -> Unit,
    onGenerate: () -> Unit,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .padding(24.dp),
        contentAlignment = Alignment.Center
    ) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(
                "LUXURY SAFARI ARCHITECT",
                color = warmCanvas,
                fontSize = 24.sp,
                fontWeight = FontWeight.Light,
                letterSpacing = 6.sp,
                textAlign = TextAlign.Center
            )

            Spacer(modifier = Modifier.height(8.dp))

            Text(
                "NATIONAL GEOGRAPHIC • LUXURY CONCIERGE",
                color = burnishedGold,
                fontSize = 10.sp,
                fontWeight = FontWeight.Medium,
                letterSpacing = 2.sp
            )

            Spacer(modifier = Modifier.height(48.dp))

            Card(
                colors = CardDefaults.cardColors(
                    containerColor = deepEvergreen.copy(alpha = 0.6f)
                ),
                shape = RoundedCornerShape(24.dp),
                border = BorderStroke(1.dp, burnishedGold.copy(alpha = 0.3f))
            ) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(24.dp)
                ) {
                    Text(
                        "DESIRE YOUR JOURNEY",
                        color = warmCanvas.copy(alpha = 0.5f),
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Medium,
                        letterSpacing = 2.sp
                    )

                    Spacer(modifier = Modifier.height(12.dp))

                    OutlinedTextField(
                        value = userRequest,
                        onValueChange = onValueChange,
                        placeholder = {
                            Text(
                                "e.g., Romantic 7-day safari in Kenya with hot air balloon",
                                color = warmCanvas.copy(alpha = 0.3f),
                                fontSize = 14.sp
                            )
                        },
                        modifier = Modifier.fillMaxWidth(),
                        colors = TextFieldDefaults.outlinedTextFieldColors(
                            focusedBorderColor = burnishedGold,
                            unfocusedBorderColor = burnishedGold.copy(alpha = 0.3f),
                            cursorColor = burnishedGold,
                            focusedTextColor = warmCanvas,
                            unfocusedTextColor = warmCanvas
                        ),
                        shape = RoundedCornerShape(16.dp)
                    )

                    Spacer(modifier = Modifier.height(24.dp))

                    Button(
                        onClick = onGenerate,
                        enabled = userRequest.isNotBlank(),
                        colors = ButtonDefaults.buttonColors(
                            containerColor = burnishedGold,
                            disabledContainerColor = burnishedGold.copy(alpha = 0.3f)
                        ),
                        shape = RoundedCornerShape(24.dp),
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(56.dp)
                    ) {
                        Text(
                            "ARCHITECT SAFARI",
                            color = deepEvergreen,
                            fontWeight = FontWeight.Medium,
                            fontSize = 14.sp,
                            letterSpacing = 2.sp
                        )
                    }
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InquiryDialog(
    operator: SafariOperator,
    itinerary: SafariItinerary?,
    onDismiss: () -> Unit,
    onConfirm: (BookingRequest) -> Unit,
    burnishedGold: Color,
    warmCanvas: Color,
    deepEvergreen: Color
) {
    var travelerName by remember { mutableStateOf("") }
    var travelerEmail by remember { mutableStateOf("") }
    var travelerPhone by remember { mutableStateOf("") }
    var travelDate by remember { mutableStateOf("") }
    var numberOfTravelers by remember { mutableStateOf(1) }
    var specialRequests by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = deepEvergreen,
        shape = RoundedCornerShape(24.dp),
        title = {
            Text(
                "REQUEST A QUOTE FROM ${operator.name.uppercase()}",
                color = burnishedGold,
                fontWeight = FontWeight.Light,
                fontSize = 16.sp,
                letterSpacing = 2.sp
            )
        },
        text = {
            Column(
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                Text(
                    "ESTIMATED TOTAL: $${operator.basePrice * numberOfTravelers}",
                    color = warmCanvas,
                    fontWeight = FontWeight.Medium,
                    fontSize = 20.sp,
                    letterSpacing = 1.sp
                )

                Text(
                    "${operator.name} will review your request and confirm exact pricing, " +
                        "availability, and dates within 24–48 hours. No payment is taken now.",
                    color = warmCanvas.copy(alpha = 0.6f),
                    fontSize = 12.sp,
                    lineHeight = 16.sp
                )

                OutlinedTextField(
                    value = travelerName,
                    onValueChange = { travelerName = it },
                    label = { Text("Full Name", color = warmCanvas.copy(alpha = 0.5f)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = TextFieldDefaults.outlinedTextFieldColors(
                        focusedBorderColor = burnishedGold,
                        unfocusedBorderColor = burnishedGold.copy(alpha = 0.3f),
                        cursorColor = burnishedGold,
                        focusedTextColor = warmCanvas,
                        unfocusedTextColor = warmCanvas
                    ),
                    shape = RoundedCornerShape(16.dp)
                )

                OutlinedTextField(
                    value = travelerEmail,
                    onValueChange = { travelerEmail = it },
                    label = { Text("Email", color = warmCanvas.copy(alpha = 0.5f)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = TextFieldDefaults.outlinedTextFieldColors(
                        focusedBorderColor = burnishedGold,
                        unfocusedBorderColor = burnishedGold.copy(alpha = 0.3f),
                        cursorColor = burnishedGold,
                        focusedTextColor = warmCanvas,
                        unfocusedTextColor = warmCanvas
                    ),
                    shape = RoundedCornerShape(16.dp)
                )

                OutlinedTextField(
                    value = travelerPhone,
                    onValueChange = { travelerPhone = it },
                    label = { Text("Phone (optional)", color = warmCanvas.copy(alpha = 0.5f)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = TextFieldDefaults.outlinedTextFieldColors(
                        focusedBorderColor = burnishedGold,
                        unfocusedBorderColor = burnishedGold.copy(alpha = 0.3f),
                        cursorColor = burnishedGold,
                        focusedTextColor = warmCanvas,
                        unfocusedTextColor = warmCanvas
                    ),
                    shape = RoundedCornerShape(16.dp),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone)
                )

                OutlinedTextField(
                    value = travelDate,
                    onValueChange = { travelDate = it },
                    label = { Text("Preferred Travel Date", color = warmCanvas.copy(alpha = 0.5f)) },
                    placeholder = { Text("e.g., August 2026", color = warmCanvas.copy(alpha = 0.3f)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = TextFieldDefaults.outlinedTextFieldColors(
                        focusedBorderColor = burnishedGold,
                        unfocusedBorderColor = burnishedGold.copy(alpha = 0.3f),
                        cursorColor = burnishedGold,
                        focusedTextColor = warmCanvas,
                        unfocusedTextColor = warmCanvas
                    ),
                    shape = RoundedCornerShape(16.dp)
                )

                OutlinedTextField(
                    value = numberOfTravelers.toString(),
                    onValueChange = { numberOfTravelers = it.toIntOrNull()?.coerceAtLeast(1) ?: 1 },
                    label = { Text("Number of Travelers", color = warmCanvas.copy(alpha = 0.5f)) },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                    colors = TextFieldDefaults.outlinedTextFieldColors(
                        focusedBorderColor = burnishedGold,
                        unfocusedBorderColor = burnishedGold.copy(alpha = 0.3f),
                        cursorColor = burnishedGold,
                        focusedTextColor = warmCanvas,
                        unfocusedTextColor = warmCanvas
                    ),
                    shape = RoundedCornerShape(16.dp),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
                )

                OutlinedTextField(
                    value = specialRequests,
                    onValueChange = { specialRequests = it },
                    label = { Text("Special Requests (optional)", color = warmCanvas.copy(alpha = 0.5f)) },
                    modifier = Modifier.fillMaxWidth(),
                    colors = TextFieldDefaults.outlinedTextFieldColors(
                        focusedBorderColor = burnishedGold,
                        unfocusedBorderColor = burnishedGold.copy(alpha = 0.3f),
                        cursorColor = burnishedGold,
                        focusedTextColor = warmCanvas,
                        unfocusedTextColor = warmCanvas
                    ),
                    shape = RoundedCornerShape(16.dp),
                    minLines = 2
                )
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    onConfirm(
                        BookingRequest(
                            operatorId = operator.id,
                            operatorName = operator.name,
                            itineraryTitle = itinerary?.title ?: "Custom Safari",
                            totalPrice = operator.basePrice * numberOfTravelers,
                            travelerName = travelerName,
                            travelerEmail = travelerEmail,
                            travelerPhone = travelerPhone,
                            travelDate = travelDate,
                            numberOfTravelers = numberOfTravelers,
                            specialRequests = specialRequests
                        )
                    )
                },
                enabled = travelerName.isNotBlank() && travelerEmail.isNotBlank(),
                colors = ButtonDefaults.buttonColors(
                    containerColor = burnishedGold,
                    disabledContainerColor = burnishedGold.copy(alpha = 0.3f)
                ),
                shape = RoundedCornerShape(16.dp)
            ) {
                Text(
                    "SEND INQUIRY",
                    color = deepEvergreen,
                    fontWeight = FontWeight.Medium,
                    fontSize = 12.sp,
                    letterSpacing = 1.sp,
                    modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)
                )
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(
                    "CANCEL",
                    color = warmCanvas.copy(alpha = 0.5f),
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Medium
                )
            }
        }
    )
}
