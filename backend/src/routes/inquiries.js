const express = require('express');
const db = require('../db/database');

const router = express.Router();

/**
 * Basic email format check — not exhaustive RFC 5322 validation, just
 * enough to reject obviously malformed input before it reaches storage
 * or (eventually) an outbound notification email.
 */
function isValidEmail(email) {
  return typeof email === 'string' && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
}

/**
 * POST /api/inquiries
 *
 * Receives an inquiry submitted from the Android app's InquiryDialog.
 * This is intentionally simple: validate, store, return a reference.
 * It does NOT process payment and does NOT confirm availability — matching
 * the honest "SEND INQUIRY" framing on the client. An operator (human, for
 * now) needs to review this record and follow up with the traveler.
 *
 * Adding Stripe later: the natural point to add a payment step is AFTER an
 * operator confirms availability and a final price — i.e. a second
 * endpoint (e.g. POST /api/inquiries/:id/payment-link) created once an
 * operator has reviewed the request, not at initial submission time. Do
 * not wire Stripe directly into this endpoint; that would recreate the
 * "instant booking with no availability check" problem this whole redesign
 * exists to fix.
 */
router.post('/', (req, res) => {
  const {
    localBookingId,
    operatorId,
    operatorName,
    itineraryTitle,
    totalPrice,
    travelerName,
    travelerEmail,
    travelerPhone,
    travelDate,
    numberOfTravelers,
    specialRequests
  } = req.body;

  const errors = [];
  if (!operatorId || typeof operatorId !== 'string') errors.push('operatorId is required');
  if (!operatorName || typeof operatorName !== 'string') errors.push('operatorName is required');
  if (!travelerName || typeof travelerName !== 'string' || travelerName.trim().length === 0) {
    errors.push('travelerName is required');
  }
  if (!isValidEmail(travelerEmail)) errors.push('a valid travelerEmail is required');
  if (typeof totalPrice !== 'number' || totalPrice < 0) errors.push('totalPrice must be a non-negative number');
  if (typeof numberOfTravelers !== 'number' || numberOfTravelers < 1) {
    errors.push('numberOfTravelers must be at least 1');
  }

  if (errors.length > 0) {
    return res.status(400).json({ error: 'Validation failed', details: errors });
  }

  try {
    const stmt = db.prepare(`
      INSERT INTO inquiries (
        local_booking_id, operator_id, operator_name, itinerary_title,
        total_price, traveler_name, traveler_email, traveler_phone,
        travel_date, number_of_travelers, special_requests
      ) VALUES (
        @localBookingId, @operatorId, @operatorName, @itineraryTitle,
        @totalPrice, @travelerName, @travelerEmail, @travelerPhone,
        @travelDate, @numberOfTravelers, @specialRequests
      )
    `);

    const result = stmt.run({
      localBookingId: localBookingId ?? 0,
      operatorId,
      operatorName,
      itineraryTitle: itineraryTitle ?? 'Custom Safari',
      totalPrice,
      travelerName: travelerName.trim(),
      travelerEmail: travelerEmail.trim().toLowerCase(),
      travelerPhone: travelerPhone ?? '',
      travelDate: travelDate ?? '',
      numberOfTravelers,
      specialRequests: specialRequests ?? ''
    });

    // NOTE: This is the natural place to trigger an outbound notification
    // (email to your ops team, Slack webhook, etc.) so a human actually
    // sees the inquiry promptly. Not implemented here since it depends on
    // which email/notification provider you use — flagged rather than
    // silently omitted.

    return res.status(201).json({
      id: result.lastInsertRowid,
      status: 'PENDING',
      message: 'Inquiry received. An operator will follow up by email within 24-48 hours.'
    });
  } catch (err) {
    console.error('Failed to insert inquiry:', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * GET /api/inquiries
 *
 * Lists inquiries, optionally filtered by status or operatorId. Intended
 * for an internal ops dashboard, not the consumer app. No auth is applied
 * here — see server.js for the ADMIN_API_KEY middleware that protects this
 * route in production.
 */
router.get('/', (req, res) => {
  const { status, operatorId } = req.query;

  let query = 'SELECT * FROM inquiries WHERE 1=1';
  const params = {};

  if (status) {
    query += ' AND status = @status';
    params.status = status;
  }
  if (operatorId) {
    query += ' AND operator_id = @operatorId';
    params.operatorId = operatorId;
  }

  query += ' ORDER BY created_at DESC LIMIT 200';

  try {
    const rows = db.prepare(query).all(params);
    return res.json({ inquiries: rows });
  } catch (err) {
    console.error('Failed to list inquiries:', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
});

/**
 * PATCH /api/inquiries/:id/status
 *
 * Updates an inquiry's status (e.g. PENDING -> CONFIRMED, or -> DECLINED).
 * This is the operator-side action after they've reviewed availability and
 * pricing manually. Also protected by ADMIN_API_KEY in production.
 */
router.patch('/:id/status', (req, res) => {
  const { id } = req.params;
  const { status } = req.body;

  const validStatuses = ['PENDING', 'CONFIRMED', 'DECLINED', 'CANCELLED'];
  if (!validStatuses.includes(status)) {
    return res.status(400).json({
      error: `status must be one of: ${validStatuses.join(', ')}`
    });
  }

  try {
    const stmt = db.prepare(`
      UPDATE inquiries
      SET status = @status, updated_at = datetime('now')
      WHERE id = @id
    `);
    const result = stmt.run({ id, status });

    if (result.changes === 0) {
      return res.status(404).json({ error: 'Inquiry not found' });
    }

    return res.json({ id: Number(id), status });
  } catch (err) {
    console.error('Failed to update inquiry status:', err);
    return res.status(500).json({ error: 'Internal server error' });
  }
});

module.exports = router;
