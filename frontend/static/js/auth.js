// Minimal auth glue (M6a).
//
// Stores the Supabase access token returned by the gateway's /auth/login and
// /auth/signup, and attaches it as a Bearer header on gateway calls. The token
// lives in localStorage (Bearer, not a cookie) because the frontend and gateway
// are separate origins behind wildcard CORS, where credentialed cookies don't
// apply; the tradeoff is XSS exposure, which is acceptable for this portfolio
// build. This is thin glue only — no application logic lives here.

const TOKEN_KEY = "mb_access_token";
const USER_KEY = "mb_user";

export function saveSession(session) {
  if (session && session.access_token) {
    localStorage.setItem(TOKEN_KEY, session.access_token);
  }
  if (session && session.user) {
    localStorage.setItem(USER_KEY, JSON.stringify(session.user));
  }
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function getUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || "null");
  } catch {
    return null;
  }
}

export function isLoggedIn() {
  return !!getToken();
}

// Merge an Authorization header onto `extra` when a token is present. Never sets
// Content-Type, so it is safe to spread onto a FormData upload.
export function authHeaders(extra = {}) {
  const token = getToken();
  return token ? { ...extra, Authorization: `Bearer ${token}` } : { ...extra };
}

export function logout(redirect = "/login") {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  if (redirect) window.location.href = redirect;
}

// Redirect to /login when unauthenticated. Returns true when a token is present
// so callers can gate page setup: `if (requireAuth()) { ...setup... }`.
export function requireAuth() {
  if (!getToken()) {
    window.location.href = "/login";
    return false;
  }
  return true;
}
