/* Local "Supabase gateway" for END-TO-END testing ONLY.
 *
 * Presents the slice of the Supabase HTTP API that supabase-js uses, backed by
 * the real native Postgres + PostgREST stack (so RLS is genuinely enforced):
 *   /auth/v1/*   -> minimal magic-link / password stubs that mint local JWTs
 *   /rest/v1/*   -> reverse-proxied to PostgREST (prefix stripped)
 * NOT part of the shipped app. Production uses real Supabase.
 */
const http = require('http');
const crypto = require('crypto');

const SECRET = 'pwcrm-local-test-secret-key-min-32-chars-1234567890';
const PGRST = { host: '127.0.0.1', port: 3999 };
const PORT = Number(process.env.GW_PORT || 4000);

// email -> user id (mirrors the profiles seeded by the E2E harness)
const USERS = {
  'owner@a.co':  '11111111-0000-0000-0000-000000000001',
  'agent@a.co':  '11111111-0000-0000-0000-000000000002',
  'owner@b.co':  '22222222-0000-0000-0000-000000000001',
  'newva@a.co':  '88888888-0000-0000-0000-0000000000aa'
};
const byRefresh = {}; // refresh_token -> sub

function b64(o) { return Buffer.from(JSON.stringify(o)).toString('base64url'); }
function mint(sub) {
  const now = Math.floor(Date.now() / 1000);
  const p = { sub, role: 'authenticated', aud: 'authenticated', iat: now, exp: now + 3600, email: subEmail(sub) };
  const data = b64({ alg: 'HS256', typ: 'JWT' }) + '.' + b64(p);
  const sig = crypto.createHmac('sha256', SECRET).update(data).digest('base64url');
  return data + '.' + sig;
}
function subEmail(sub) { return Object.keys(USERS).find(e => USERS[e] === sub) || ''; }
function decodeSub(auth) {
  try {
    const tok = (auth || '').replace(/^Bearer\s+/i, '');
    const p = JSON.parse(Buffer.from(tok.split('.')[1], 'base64url').toString());
    return p.sub;
  } catch (e) { return null; }
}
function session(sub) {
  const refresh = crypto.randomBytes(12).toString('hex');
  byRefresh[refresh] = sub;
  return {
    access_token: mint(sub), token_type: 'bearer', expires_in: 3600,
    expires_at: Math.floor(Date.now() / 1000) + 3600, refresh_token: refresh,
    user: { id: sub, aud: 'authenticated', role: 'authenticated', email: subEmail(sub) }
  };
}
function readBody(req) {
  return new Promise(res => { let b = ''; req.on('data', c => b += c); req.on('end', () => res(b)); });
}
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET,POST,PATCH,DELETE,OPTIONS',
  'Access-Control-Allow-Headers': 'authorization,apikey,content-type,prefer,x-client-info,accept-profile,content-profile,range,x-supabase-api-version',
  'Access-Control-Expose-Headers': 'content-range,content-location'
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (req.method === 'OPTIONS') { res.writeHead(204, CORS); return res.end(); }

  // ---- AUTH ----
  if (url.pathname.startsWith('/auth/v1/')) {
    const body = await readBody(req);
    let json = {}; try { json = body ? JSON.parse(body) : {}; } catch (e) {}
    const path = url.pathname.replace('/auth/v1/', '');

    if (path === 'token') {
      const grant = url.searchParams.get('grant_type');
      let sub = null;
      if (grant === 'password') sub = USERS[(json.email || '').toLowerCase()];
      else if (grant === 'refresh_token') sub = byRefresh[json.refresh_token];
      if (!sub) { res.writeHead(400, Object.assign({ 'Content-Type': 'application/json' }, CORS)); return res.end(JSON.stringify({ error: 'invalid_grant', error_description: 'Invalid login' })); }
      res.writeHead(200, Object.assign({ 'Content-Type': 'application/json' }, CORS));
      return res.end(JSON.stringify(session(sub)));
    }
    if (path === 'signup') {
      const sub = USERS[(json.email || '').toLowerCase()];
      res.writeHead(sub ? 200 : 400, Object.assign({ 'Content-Type': 'application/json' }, CORS));
      return res.end(JSON.stringify(sub ? session(sub) : { error: 'unknown', msg: 'test users only' }));
    }
    if (path === 'user') {
      const sub = decodeSub(req.headers.authorization);
      if (!sub) { res.writeHead(401, CORS); return res.end('{}'); }
      res.writeHead(200, Object.assign({ 'Content-Type': 'application/json' }, CORS));
      return res.end(JSON.stringify({ id: sub, aud: 'authenticated', role: 'authenticated', email: subEmail(sub) }));
    }
    if (path === 'logout') { res.writeHead(204, CORS); return res.end(); }
    if (path === 'magiclink' || path === 'otp') { // pretend we emailed a link
      res.writeHead(200, Object.assign({ 'Content-Type': 'application/json' }, CORS)); return res.end('{}');
    }
    res.writeHead(200, Object.assign({ 'Content-Type': 'application/json' }, CORS)); return res.end('{}');
  }

  // ---- REST -> PostgREST ----
  if (url.pathname.startsWith('/rest/v1/')) {
    const body = await readBody(req);
    const pgPath = url.pathname.replace('/rest/v1', '') + url.search;
    const headers = {};
    ['authorization', 'content-type', 'prefer', 'accept', 'range', 'accept-profile', 'content-profile'].forEach(h => {
      if (req.headers[h]) headers[h] = req.headers[h];
    });
    const preq = http.request({ host: PGRST.host, port: PGRST.port, path: pgPath, method: req.method, headers }, pres => {
      const h = Object.assign({}, CORS);
      ['content-type', 'content-range', 'content-location'].forEach(k => { if (pres.headers[k]) h[k] = pres.headers[k]; });
      res.writeHead(pres.statusCode, h);
      pres.pipe(res);
    });
    preq.on('error', e => { res.writeHead(502, CORS); res.end(JSON.stringify({ message: String(e) })); });
    if (body) preq.write(body);
    preq.end();
    return;
  }

  res.writeHead(404, CORS); res.end('not found');
});
server.listen(PORT, () => console.log('gateway on http://127.0.0.1:' + PORT));
