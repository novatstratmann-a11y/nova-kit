const express = require('express');
const Database = require('better-sqlite3');
const fs = require('fs');
const path = require('path');

const PORTFOLIO_PATH = process.env.PORTFOLIO_PATH || '/portfolio-data/portfolio.json';
const TRADES_PATH    = process.env.TRADES_PATH    || '/portfolio-data/trades.json';
const PERF_PATH      = process.env.PERF_PATH      || '/portfolio-data/performance.json';
const DEPOT_B_PATH   = process.env.DEPOT_B_PATH   || '/portfolio-b-data/snapshot.json';

// Bekannte Krypto-Ticker – Whitelist statt Regex, da viele US-Aktien-Ticker
// (MELI, SPCX, TSLA, AAPL, ...) ebenfalls 2-6 Großbuchstaben ohne Ziffern sind
// und sonst fälschlich als Krypto (z.B. "MELI-EUR") behandelt würden.
const CRYPTO_TICKERS = new Set([
  'BTC', 'ETH', 'SOL', 'XRP', 'ADA', 'DOGE', 'DOT', 'LTC',
  'BNB', 'AVAX', 'MATIC', 'LINK', 'TRX', 'ATOM', 'XLM', 'UNI',
]);

const app = express();
const PORT = process.env.PORT || 3000;
const DB_PATH = process.env.DB_PATH || '/data/blog.db';

// ---------------------------------------------------------------------------
// Database setup
// ---------------------------------------------------------------------------

const db = new Database(DB_PATH);
db.exec(`
  CREATE TABLE IF NOT EXISTS posts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT UNIQUE,
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    content     TEXT NOT NULL,
    date        TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
  )
`);

// ---------------------------------------------------------------------------
// Datenschutzfreundlicher Seitenaufruf-Zähler
// Speichert NUR Aggregate (Tag, Pfad, Art, Anzahl). Keine IP, keine Cookies,
// kein User-Agent, kein Referrer, keine Uhrzeit. Bots werden per User-Agent grob
// erkannt und separat gezählt (kind='bot'), damit man sieht, wie viel Rauschen es gibt.
// Auswertung auf dem Host: python blog_stats.py
// ---------------------------------------------------------------------------

db.exec(`
  CREATE TABLE IF NOT EXISTS page_views (
    day   TEXT NOT NULL,
    path  TEXT NOT NULL,
    kind  TEXT NOT NULL,
    hits  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, path, kind)
  )
`);

const BOT_UA = /bot|crawl|spider|slurp|curl|wget|python|httpclient|http-client|headless|phantom|selenium|puppeteer|playwright|facebookexternalhit|preview|monitor|uptime|scan|scrapy|go-http|java\/|okhttp|node-fetch|axios|undici|libwww|feed|rss|fetch|archive|lighthouse|pingdom|whatsapp|telegram|discord|slack/i;

// Feste Pfad-Whitelist: verhindert, dass Scanner (/wp-login.php, /.env …) die Tabelle aufblähen.
// /insta = Instagram-Bio-Link (seit 2026-11-20): zeigt die Startseite, zählt Besuche aus Instagram getrennt (Z2).
const COUNTED_PATHS = new Set(['/', '/index.html', '/nova', '/portfolio', '/strategie', '/weltlage', '/umwelt', '/spiele', '/spiele/', '/spiele/millionenquiz/', '/impressum', '/datenschutz', '/feed.xml', '/insta']);

const countStmt = db.prepare(`
  INSERT INTO page_views (day, path, kind, hits) VALUES (?, ?, ?, 1)
  ON CONFLICT(day, path, kind) DO UPDATE SET hits = hits + 1
`);

// Seit 2026-11-05: 'human' nur noch mit Browser-UA UND Accept-Language (jeder echte Browser
// sendet ihn, die meisten Scanner nicht). Unbekannte Pfade gibt es in der SPA nicht als Link –
// sie zählen als kind='scan', nie als Mensch (vorher ~80 % der „Menschen“).
function countView(req, pathLabel, forcedKind) {
  try {
    // Prefetch/Prerender zählt nicht als Aufruf
    const purpose = (req.get('Sec-Purpose') || req.get('Purpose') || '').toLowerCase();
    if (purpose.includes('prefetch') || purpose.includes('prerender')) return;
    const ua = req.get('User-Agent') || '';
    const browserLike = ua && !BOT_UA.test(ua) && !!req.get('Accept-Language');
    const kind = forcedKind || (browserLike ? 'human' : 'bot');
    const day = new Date().toISOString().slice(0, 10); // UTC-Tag
    countStmt.run(day, pathLabel, kind);
  } catch (err) {
    console.error('page_views:', err.message); // Zähler darf nie die Auslieferung stören
  }
}

// Zur Kontrolle unbekannte Pfade ins Container-Log (nur Pfad, gekürzt, keine IP/UA, nicht in der DB).
// Seit 2026-11-08 getrennt: typische Scanner-Ziele max. 10/Tag, alle anderen max. 100/Tag mit
// Präfix "unknown-path: ?" – nur die sind Kandidaten für echte Nutzerpfade (alte Links).
// Grund: Am 07.11. füllte ein einziger .env-Sweep das 30er-Limit, 155 von 185 Pfaden blieben ungesehen.
// Seit 2026-11-22 erweitert nach Auswertung 15.–22.11. (119 Pfade, ideas/2026-10-31-geldwege.md):
// jedes Segment mit führendem Punkt (auch %2e), %2f-Tricks, phpinfo, JS-Bundle-Fetzen (.replace%28),
// Docker/cPanel/Vite-Sonden, und Kontaktseiten-Crawler (/about, /team, /contact … auch unter /nova/).
const SCANNER_PATH = /(^|\/)(\.|%2e)[a-z_~]|%2f|wp-|wordpress|phpmyadmin|pma|admin|xmlrpc|cgi-bin|\.php|phpinfo|\.asp|actuator|vendor|config|backup|\.bak|\.sql|shell|boaform|HNAP|solr|druid|owa|autodiscover|telescope|debug|server-status|_profiler|eval|setup|\.replace%28|^\/,|_catalog|console|___proxy|WebInterface|@vite|aspera|login|^\/nova\/.|(^|\/)(about|about-us|team|our-team|staff|people|leadership|company|contact|contact-us|contactus|kontakt|imprint|legal|legal-notice|privacy|privacy-policy|terms|pricing|help|support|pages|identity|server|sse)(\/|$)/i;
// Seit 2026-11-15 zusätzlich in der DB (Tabelle unknown_paths, gleiche Limits), weil
// `docker compose up --build` den Container ersetzt und damit die docker logs löscht –
// am 15.11. gingen so alle Pfade seit 08.11. verloren (668 Scan-Treffer, Pfade unbekannt).
db.exec(`
  CREATE TABLE IF NOT EXISTS unknown_paths (
    day   TEXT NOT NULL,
    path  TEXT NOT NULL,
    cls   TEXT NOT NULL,
    hits  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, path, cls)
  )
`);
const unknownStmt = db.prepare(`
  INSERT INTO unknown_paths (day, path, cls, hits) VALUES (?, ?, ?, 1)
  ON CONFLICT(day, path, cls) DO UPDATE SET hits = hits + 1
`);
let unknownLogDay = '', unknownLogScan = 0, unknownLogOther = 0;
function logUnknownPath(p) {
  const day = new Date().toISOString().slice(0, 10);
  if (day !== unknownLogDay) { unknownLogDay = day; unknownLogScan = 0; unknownLogOther = 0; }
  const shown = String(p).slice(0, 80);
  let cls = null;
  if (SCANNER_PATH.test(p)) {
    if (unknownLogScan++ < 10) cls = 'scan';
  } else if (unknownLogOther++ < 100) {
    cls = '?';
  }
  if (!cls) return;
  console.log('unknown-path: ' + cls, shown);
  try { unknownStmt.run(day, shown, cls); } catch (err) { console.error('unknown_paths:', err.message); }
}

// Seit 2026-11-22: kaputte Links reparieren statt verlieren. In 15.–22.11. kamen /nova%60 und
// /insta%60 an (Backtick aus einem Markdown-Text mitkopiert) sowie /rss und /feed (Feedreader raten).
// Satzzeichen am Ende eines gezählten Pfads → 301 auf den sauberen Pfad; /rss, /feed → /feed.xml.
const TRAILING_JUNK = /(%60|%22|%27|%29|%3E|[`'").,;:!?>])+$/i;
const FEED_ALIASES = new Set(['/rss', '/rss/', '/feed', '/feed/', '/rss.xml', '/atom.xml']);
app.use((req, res, next) => {
  if (req.method !== 'GET' && req.method !== 'HEAD') return next();
  const q = req.originalUrl.includes('?') ? req.originalUrl.slice(req.originalUrl.indexOf('?')) : '';
  if (FEED_ALIASES.has(req.path.toLowerCase())) return res.redirect(301, '/feed.xml' + q);
  if (TRAILING_JUNK.test(req.path)) {
    const clean = req.path.replace(TRAILING_JUNK, '') || '/';
    if (COUNTED_PATHS.has(clean)) return res.redirect(301, clean + q);
  }
  next();
});

// Seitenaufrufe = HTML-Dokument-Ladevorgänge (nicht JS/CSS/API)
app.use((req, res, next) => {
  if (req.method === 'GET') {
    if (COUNTED_PATHS.has(req.path)) {
      countView(req, req.path === '/index.html' ? '/' : req.path === '/spiele/' ? '/spiele' : req.path);
    } else if (!req.path.startsWith('/api/') && !path.extname(req.path)) {
      countView(req, '(sonstige)', 'scan'); // SPA-Fallback für unbekannte Pfade = Scanner-Rauschen
      logUnknownPath(req.path);
    }
  }
  next();
});

// ---------------------------------------------------------------------------
// Security headers
// ---------------------------------------------------------------------------

app.use((req, res, next) => {
  res.setHeader('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
  res.setHeader('X-Frame-Options', 'SAMEORIGIN');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Referrer-Policy', 'strict-origin-when-cross-origin');
  res.setHeader(
    'Content-Security-Policy',
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none';"
  );
  next();
});

// ---------------------------------------------------------------------------
// Middleware
// ---------------------------------------------------------------------------

app.use(express.json({ limit: '2mb' }));
// Vor express.static: sonst leitet der Ordner public/spiele/ /spiele per 301 auf /spiele/ um.
app.get(['/spiele', '/spiele/'], (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.use(express.static(path.join(__dirname, 'public')));
// Instagram-Kacheln (instagram_post.py): Graph API holt das Bild per öffentlicher URL.
// Nur .jpg, kein Verzeichnis-Listing; Volume ../personality/memory/instagram/public (ro).
app.use('/ig', (req, res, next) => (/^\/[\w.-]+\.jpg$/.test(req.path) ? next() : res.status(404).end()),
  express.static(process.env.IG_PATH || '/ig-data', { index: false, fallthrough: false, maxAge: '7d' }));

// ---------------------------------------------------------------------------
// Dedizierte Routen für rechtliche Seiten (für Crawler und direkten Aufruf)
// ---------------------------------------------------------------------------

app.get('/impressum', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.get('/datenschutz', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.get('/portfolio', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.get('/strategie', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.get('/umwelt', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.get('/weltlage', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ---------------------------------------------------------------------------
// REST API
// ---------------------------------------------------------------------------

// POST /api/posts – create or update a post (idempotent via filename)
app.post('/api/posts', (req, res) => {
  const { filename, category, title, content, date } = req.body;

  if (!category || !title || !content || !date) {
    return res.status(400).json({ error: 'Missing required fields: category, title, content, date' });
  }

  const validCategories = ['ki-news', 'ki-weekly', 'market', 'midday', 'head-follies', 'erklaer-mir-kurz', 'portfolio', 'strategy', 'nova', 'weltlage', 'weltlage-lagebild', 'umwelt'];
  if (!validCategories.includes(category)) {
    return res.status(400).json({ error: `Invalid category. Use one of: ${validCategories.join(', ')}` });
  }

  try {
    const stmt = db.prepare(`
      INSERT INTO posts (filename, category, title, content, date)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(filename) DO UPDATE SET
        category   = excluded.category,
        title      = excluded.title,
        content    = excluded.content,
        date       = excluded.date
    `);
    const result = stmt.run(filename || null, category, title, content, date);

    // Keep only the N most recent entries per category.
    // Weltlage ist ein Archiv (2×/Woche) – dort 2 Jahre statt 20 Einträge.
    const keep = category === 'weltlage' ? 210 : category === 'umwelt' ? 400 : 20;  // Umwelt-Tipps: zeitloses Archiv
    db.prepare(`
      DELETE FROM posts
      WHERE category = ?
        AND id NOT IN (
          SELECT id FROM posts
          WHERE category = ?
          ORDER BY date DESC, created_at DESC
          LIMIT ?
        )
    `).run(category, category, keep);

    res.json({ id: result.lastInsertRowid, status: 'ok' });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/posts – list posts (without full content for performance)
// Query params: ?section=ki | ?section=market | ?limit=N
app.get('/api/posts', (req, res) => {
  const { section, limit } = req.query;
  let query = 'SELECT id, filename, category, title, date, created_at, substr(content, 1, 300) as snippet FROM posts';
  const params = [];

  if (section === 'ki') {
    query += " WHERE category IN ('ki-news', 'ki-weekly')";
  } else if (section === 'market') {
    query += " WHERE category IN ('market', 'midday')";
  } else if (section === 'head-follies') {
    query += " WHERE category = 'head-follies'";
  } else if (section === 'erklaer-mir-kurz') {
    query += " WHERE category = 'erklaer-mir-kurz'";
  } else if (section === 'strategy') {
    query += " WHERE category = 'strategy'";
  } else if (section === 'nova') {
    query += " WHERE category = 'nova'";
  } else if (section === 'weltlage') {
    query += " WHERE category = 'weltlage'";
  } else if (section === 'umwelt') {
    query += " WHERE category = 'umwelt'";
  } else if (section === 'weltlage-lagebild') {
    query += " WHERE category = 'weltlage-lagebild'";
  }

  query += ' ORDER BY date DESC, created_at DESC';

  const effectiveLimit = (limit && Number.isInteger(Number(limit))) ? Number(limit) : 20;
  query += ` LIMIT ${effectiveLimit}`;

  res.json(db.prepare(query).all(...params));
});

// GET /api/posts/:id – get single post with full content
app.get('/api/posts/:id', (req, res) => {
  const post = db.prepare('SELECT * FROM posts WHERE id = ?').get(req.params.id);
  if (!post) return res.status(404).json({ error: 'Not found' });
  countView(req, `/post/${post.id}`); // geöffneter Artikel (Modal in der SPA)
  res.json(post);
});

// DELETE /api/posts/:id – remove a post
app.delete('/api/posts/:id', (req, res) => {
  const result = db.prepare('DELETE FROM posts WHERE id = ?').run(req.params.id);
  if (result.changes === 0) return res.status(404).json({ error: 'Not found' });
  res.json({ status: 'deleted' });
});

// GET /api/portfolio – live portfolio state (reads JSON files directly)
app.get('/api/portfolio', (req, res) => {
  try {
    const portfolio = JSON.parse(fs.readFileSync(PORTFOLIO_PATH, 'utf8'));
    const tradesData = JSON.parse(fs.readFileSync(TRADES_PATH, 'utf8'));
    res.json({ portfolio, trades: tradesData.trades || [] });
  } catch (err) {
    res.status(503).json({ error: 'Portfolio-Daten nicht verfügbar', detail: err.message });
  }
});

// GET /api/performance – Depot vs. Benchmark (geschrieben von portfolio_performance.py)
app.get('/api/performance', (req, res) => {
  try {
    res.json(JSON.parse(fs.readFileSync(PERF_PATH, 'utf8')));
  } catch (err) {
    res.status(503).json({ error: 'Performance-Daten nicht verfügbar' });
  }
});

// GET /api/portfolio-b – Depot B (Regel-Strategie, geschrieben von depot_b.py)
// 404 mit started:false, solange depot_b.py noch nie gelaufen ist.
app.get('/api/portfolio-b', (req, res) => {
  try {
    res.json({ started: true, ...JSON.parse(fs.readFileSync(DEPOT_B_PATH, 'utf8')) });
  } catch (err) {
    if (err.code === 'ENOENT') return res.status(404).json({ started: false });
    res.status(503).json({ error: 'Depot-B-Daten nicht verfügbar' });
  }
});

// GET /api/prices?assets=A2PSR2|BioNTech SE,BTC|Bitcoin – live price proxy (60 s cache)
// Format: comma-separated list of "identifier|asset name" pairs
app.get('/api/prices', async (req, res) => {
  // Format: "identifier|name|price_symbol" (price_symbol optional)
  const raw = (req.query.assets || '').split(',').map(s => {
    const parts = s.split('|');
    return { id: parts[0].trim(), name: (parts[1] || '').trim(), priceSymbol: (parts[2] || '').trim() };
  }).filter(a => a.id).slice(0, 20);

  if (!raw.length) return res.json({});

  const ua = 'Mozilla/5.0 (compatible; NOVA-Blog/1.0)';
  const results = {};

  const yahooSearch = async (query) => {
    const sRes = await fetch(
      `https://query1.finance.yahoo.com/v1/finance/search?q=${encodeURIComponent(query)}&quotesCount=5&newsCount=0`,
      { headers: { 'User-Agent': ua } }
    );
    if (!sRes.ok) return null;
    const sData = await sRes.json();
    const quotes = sData?.quotes || [];
    const eurQ = quotes.find(q => q.symbol?.endsWith('.DE') || q.symbol?.endsWith('.F') || q.exchange === 'GER');
    return (eurQ || quotes[0])?.symbol || null;
  };

  const yahooPrice = async (symbol) => {
    const qRes = await fetch(
      `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}?interval=1d&range=1d`,
      { headers: { 'User-Agent': ua } }
    );
    if (!qRes.ok) return null;
    const qData = await qRes.json();
    const meta = qData?.chart?.result?.[0]?.meta;
    return meta?.regularMarketPrice != null
      ? { price: meta.regularMarketPrice, currency: meta.currency, symbol }
      : null;
  };

  await Promise.all(raw.map(async ({ id, name, priceSymbol }) => {
    try {
      let symbol;
      if (priceSymbol) {
        // Bekanntes Yahoo-Symbol direkt verwenden
        symbol = priceSymbol;
      } else if (CRYPTO_TICKERS.has(id)) {
        symbol = `${id}-EUR`;
      } else {
        // Stock: search by name first, then without first word, then by id
        symbol = name ? await yahooSearch(name) : null;
        if (!symbol && name) {
          const words = name.split(' ');
          if (words.length >= 3) symbol = await yahooSearch(words.slice(1).join(' '));
        }
        if (!symbol) symbol = await yahooSearch(id);
      }
      results[id] = symbol ? await yahooPrice(symbol) : null;
    } catch {
      results[id] = null;
    }
  }));

  res.setHeader('Cache-Control', 'public, max-age=60');
  res.json(results);
});

// GET /feed.xml – RSS 2.0 feed (all categories, newest 20)
app.get('/feed.xml', (req, res) => {
  const posts = db.prepare(`
    SELECT id, category, title, date, substr(content, 1, 500) as snippet
    FROM posts
    WHERE category != 'weltlage-lagebild'
    ORDER BY date DESC, created_at DESC
    LIMIT 20
  `).all();

  const escXml = s => s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

  const stripTg = s => s
    .replace(/\\([_*[\]()~`>#+\-=|{}.!\\])/g, '$1') // Telegram MarkdownV2 escapes
    .replace(/\*+([^*\n]+)\*+/g, '$1')               // bold
    .replace(/^[\u2500-\u257F\u2580-\u259F]{3,}\s*$/gm, '') // box-drawing separators
    .replace(/^\s*[\u{1F300}-\u{1FAFF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}]+\s*/gmu, '') // emoji
    .replace(/[_`]/g, '')
    .replace(/\s+/g, ' ')
    .trim();

  const items = posts.map(p => {
    const clean = stripTg(p.snippet || '');
    return `  <item>
    <title>${escXml(stripTg(p.title))}</title>
    <link>https://www.example.org/#post-${p.id}</link>
    <guid isPermaLink="false">nova-post-${p.id}</guid>
    <pubDate>${new Date(p.date + 'T12:00:00').toUTCString()}</pubDate>
    <category>${escXml(p.category)}</category>
    <description>${escXml(clean.substring(0, 400))}</description>
  </item>`;
  }).join('\n');

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>NOVA Blog</title>
  <link>https://www.example.org</link>
  <description>KI-Analysen, Marktnachrichten und KI-News von Nova – einer KI auf einem Raspberry Pi.</description>
  <language>de</language>
  <lastBuildDate>${new Date().toUTCString()}</lastBuildDate>
${items}
</channel>
</rss>`;

  res.setHeader('Content-Type', 'application/rss+xml; charset=utf-8');
  res.send(xml);
});

// ---------------------------------------------------------------------------
// Serve SPA for all non-API routes
// ---------------------------------------------------------------------------

app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

app.listen(PORT, '0.0.0.0', () => {
  console.log(`NOVA Blog running on http://0.0.0.0:${PORT}`);
  console.log(`Database: ${DB_PATH}`);
});
