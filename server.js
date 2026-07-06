require('dotenv').config();

const express = require('express');
const cors = require('cors');
const rateLimit = require('express-rate-limit');

const inquiriesRouter = require('./routes/inquiries');
const { requireAdminAuth } = require('./middleware/adminAuth');

const app = express();
const PORT = process.env.PORT || 3000;

// CORS: allow requests from anywhere by default (mobile apps don't send an
// Origin header the way browsers do, so this is mostly relevant if you
// ever add a web admin dashboard). Restrict via ALLOWED_ORIGIN if needed.
const allowedOrigin = process.env.ALLOWED_ORIGIN;
app.use(cors(allowedOrigin ? { origin: allowedOrigin } : {}));

app.use(express.json({ limit: '100kb' }));

// Rate limiting on the public inquiry-submission endpoint specifically,
// to prevent it being used to spam operators or flood the database.
const inquiryLimiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 20, // 20 submissions per IP per window
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'Too many inquiries submitted. Please try again later.' }
});

app.get('/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// POST /api/inquiries is public (the app's normal flow); GET and PATCH
// under the same router are admin-only, protected inside inquiries.js's
// own route definitions is NOT how this is split — instead we mount two
// separate middleware chains against the same router path here, applying
// rate limiting only to POST and auth only to GET/PATCH via method checks
// inside the router itself would be messier, so we split by full path
// instead below for clarity.

app.use('/api/inquiries', inquiryLimiter, (req, res, next) => {
  // Public: POST (submit new inquiry) — no admin auth required.
  if (req.method === 'POST') return next();
  // Everything else on this path (GET list, PATCH status) requires admin auth.
  return requireAdminAuth(req, res, next);
}, inquiriesRouter);

app.use((req, res) => {
  res.status(404).json({ error: 'Not found' });
});

app.use((err, req, res, next) => {
  console.error('Unhandled error:', err);
  res.status(500).json({ error: 'Internal server error' });
});

app.listen(PORT, () => {
  console.log(`Africa Safari Guide backend listening on port ${PORT}`);
  if (!process.env.ADMIN_API_KEY) {
    console.warn('⚠️  ADMIN_API_KEY not set — admin endpoints are unprotected. Set it before deploying.');
  }
});
