/**
 * Simple bearer-token auth for internal/ops-only endpoints (listing
 * inquiries, updating status). Not meant for the public inquiry-submission
 * endpoint, which stays open since it's the app's normal user flow.
 *
 * Set ADMIN_API_KEY in your environment (see .env.example). Requests must
 * include: Authorization: Bearer <ADMIN_API_KEY>
 *
 * This is intentionally minimal — a single shared secret, not per-user
 * accounts. Fine for a small internal ops dashboard used by one team;
 * upgrade to real auth (e.g. per-user tokens) before giving multiple
 * external parties access.
 */
function requireAdminAuth(req, res, next) {
  const adminKey = process.env.ADMIN_API_KEY;

  if (!adminKey) {
    console.warn(
      'ADMIN_API_KEY is not set — admin routes are running with NO auth. ' +
      'Set ADMIN_API_KEY in your environment before deploying to production.'
    );
    return next();
  }

  const authHeader = req.headers.authorization || '';
  const token = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : null;

  if (token !== adminKey) {
    return res.status(401).json({ error: 'Unauthorized' });
  }

  return next();
}

module.exports = { requireAdminAuth };
