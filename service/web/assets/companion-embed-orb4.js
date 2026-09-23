/* ==========================================================================
   RHOBEAR COMPANION \u2014 drop-in chatbot embed  ("Rho")  v2
   --------------------------------------------------------------------------
   One self-contained file. Adds the living Rho orb + the FULL chatbot to ANY
   page \u2014 no build, no deps, no framework:

     - Chat panel with the Adobe chat bus: plus menu (attach image / new chat /
       copy), deep-thinking toggle, dictate button (speech-to-text INTO the
       composer \u2014 stays in normal chat), send button.
     - "Talk to Rho" call button → THE BIG ONE: fullscreen voice surface.
       Continuous listening, streamed replies spoken aloud sentence-by-sentence
       (POST /api/tts), tap the orb to interrupt (/api/interrupt).
     - Live crew visibility: tool / agent / task SSE events render as working
       chips in the thread, marked done as results land.
     - Personalize: accent color + voice picker (persisted per browser).
     - Living orb: shimmer, hue drift, breath \u2014 never a stagnant dot.

   MOUNT:
     <script defer src="/companion-embed.js"></script>
     <script>
       window.RHOBEAR_COMPANION = {
         endpoint: 'https://workbench.rhobear.ai/companion',
         ready:    true,
         accent:   '#7c5cff',  // surface accent (user may re-tint in settings)
         title:    'Rho',
         greeting: "Hey \u2014 I'm Rho. Ask me anything about your RHOBEAR."
       };
     </script>

   Transport matches the companion server exactly: POST {endpoint}/api/chat with
   { text, sessionId, chatId, mode, image? } -> SSE stream of
   `event: session|delta|tool|tool_done|agent|task|done|error|close`.
   Sellable-by-default: no keys, no founder identity, no product secrets here.
   ========================================================================== */
(function () {
  'use strict';
  if (window.__rhoEmbedLoaded) return;      // never double-mount
  window.__rhoEmbedLoaded = '2.5';

  // ---- config -------------------------------------------------------------
  var scriptEl = document.currentScript;
  function attr(name, fallback) {
    var v = scriptEl && scriptEl.getAttribute('data-' + name);
    return v == null ? fallback : v;
  }
  var CFG = window.RHOBEAR_COMPANION || {};
  var ENDPOINT = (CFG.endpoint != null ? CFG.endpoint : attr('endpoint', '')).replace(/\/+$/, '');
  var READY    = CFG.ready != null ? !!CFG.ready : (attr('ready', 'false') === 'true');
  var TITLE    = CFG.title   || attr('title', 'Rho');

  // ---- surface: which RHOBEAR app is Rho riding? -------------------------
  // Rho carries one identity everywhere; the surface controls the live orb
  // theme and the chrome accent (user bubble + send), and names the header
  // subtitle "riding the <X>".
  var SURFACE_MAP = { hub: 'the Hub', builds: 'Builds', plans: 'Plans', designs: 'Designs', capturd: "Captur'd", reviews: 'Reviews', sales: 'Sales', lab: 'the Lab' };
  function detectSurface() {
    var s = (CFG.surface || attr('surface', '') || '').toLowerCase();
    if (s && SURFACE_MAP[s]) return s;
    var h = '';
    try { h = (location.hostname || '').toLowerCase(); } catch (e) {}
    if (/workbench|hub/.test(h)) return 'hub';
    if (/plans|cloud/.test(h)) return 'plans';
    if (/designs/.test(h)) return 'designs';
    if (/capturd|captur/.test(h)) return 'capturd';
    if (/reviews/.test(h)) return 'reviews';
    if (/sales/.test(h)) return 'sales';
    if (/lab/.test(h)) return 'lab';
    return 'hub';
  }
  var SURFACE = detectSurface();
  var SURFACE_LABEL = SURFACE_MAP[SURFACE] || 'the Hub';
  // Unicode escapes (\u2014 etc) everywhere below: hosts may serve this file
  // without a UTF-8 charset header and raw em-dashes render as mojibake.
  var GREETING = CFG.greeting || attr('greeting', "Hey \u2014 I'm " + TITLE + ". Ask me anything.");
  var WARMING  = "I'm warming up \u2014 the crew's plugging me in right here. Almost ready. Hang tight and I'll be answering in this very bar.";

  // Personalization survives reloads; the surface accent is only the default.
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  // Pack law: the live orb renderer owns the surface-specific orb body; the
  // chrome accent (user bubble + send + glow) also tints to the host surface,
  // per rho.css's [data-rho-surface] map. Section labels + the "riding"
  // subtitle stay the shared teal #2A8FA8. The teal default covers unknowns.
  var SURFACE_ACCENT_MAP = { hub: '#2A8FA8', builds: '#2A8FA8', plans: '#C84BAA', designs: '#C84B4B', capturd: '#4B7AC8', reviews: '#D4A843', sales: '#8FA82A', lab: '#6B2FA8' };
  var SURFACE_ACCENT = SURFACE_ACCENT_MAP[SURFACE] || '#2A8FA8';
  var ACCENT = lsGet('rho.accent') || SURFACE_ACCENT;
  var VOICES = ['Charon', 'Puck', 'Kore', 'Fenrir', 'Aoede', 'Leda', 'Orus', 'Zephyr'];
  var VOICE = lsGet('rho.voice') || 'Charon';
  // Swatch palette = the host-surface accent + the pack per-surface accents.
  var SWATCHES = [SURFACE_ACCENT, '#2A8FA8', '#C84BAA', '#C84B4B', '#4B7AC8', '#D4A843', '#8FA82A', '#6B2FA8'];

  // Derive the companion hues from the accent so every gradient is harmonious
  // on ANY chosen color (config `accent2` can still override the default).
  function hueShift(hex, deg, satBoost, lightBoost) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
    if (!m) return hex;
    var n = parseInt(m[1], 16), r = (n >> 16) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255;
    var mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn, h = 0;
    var l = (mx + mn) / 2, s = d === 0 ? 0 : d / (1 - Math.abs(2 * l - 1));
    if (d !== 0) {
      if (mx === r) h = 60 * (((g - b) / d) % 6);
      else if (mx === g) h = 60 * ((b - r) / d + 2);
      else h = 60 * ((r - g) / d + 4);
    }
    h = (h + deg + 360) % 360;
    s = Math.min(1, Math.max(0, s + (satBoost || 0)));
    l = Math.min(0.9, Math.max(0.1, l + (lightBoost || 0)));
    var c = (1 - Math.abs(2 * l - 1)) * s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), mm = l - c / 2;
    var rr = [c, x, 0, 0, x, c][Math.floor(h / 60) % 6], gg = [x, c, c, x, 0, 0][Math.floor(h / 60) % 6], bb = [0, 0, x, c, c, x][Math.floor(h / 60) % 6];
    function ch(v) { return ('0' + Math.round((v + mm) * 255).toString(16)).slice(-2); }
    return '#' + ch(rr) + ch(gg) + ch(bb);
  }

  // ---- state --------------------------------------------------------------
  var sessionId = null;
  try { sessionId = localStorage.getItem('rho.session') || null; } catch (e) {}
  var activeAbort = null, activeChatId = null, greeted = false;
  var ttsAbort = null; // AbortController for in-flight TTS fetch (race fix)
  var pendingImage = null; // data URL waiting to ride the next send

  // ---- identity: Rho is tied to RHOBEAR credits ----------------------------
  // Same-origin hosts ride the cw_sess cookie automatically; cross-origin
  // embeds (Plans, the desktop hub) hold a companion-minted bearer token
  // handed back by the /api/auth/start popup (postMessage, origin-checked).
  var TOKEN = lsGet('rho.token') || null;
  var auth = { required: false, signedIn: true, checked: false, signin: '' };
  var EP_ORIGIN = (function () {
    try { return new URL(ENDPOINT || '/', location.href).origin; } catch (e) { return location.origin; }
  })();
  // Builds is a first-party surface. Its app shell owns the RHOBEAR session,
  // so the companion must never create a second Google-only sign-in gate.
  // Cross-origin hosts keep the companion token flow below for compatibility.
  var USE_CENTRAL_HOST_AUTH = SURFACE === 'builds' && EP_ORIGIN === location.origin;
  // Premium plasma orb art (photoreal, state-driven). Served by the companion
  // server under {endpoint}/assets/orb/*.png so BOTH same-origin (workbench) and
  // cross-origin (Plans) embeds load the exact same art from one place.
  var ORB_BASE = (ENDPOINT || '') + '/assets/orb';
  function authHeaders() {
    var h = { 'Content-Type': 'application/json' };
    if (TOKEN) h['Authorization'] = 'Bearer ' + TOKEN;
    return h;
  }
  var GOOGLE_G = '<svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>';

  // ---- styles -------------------------------------------------------------
  var css = `
  @property --rho-edge-angle { syntax: '<angle>'; initial-value: 200deg; inherits: false; }
  #rho-embed, #rho-embed * { box-sizing: border-box; }
  #rho-embed {
    /* ── _FIREFLY pack tokens (rho.css, verbatim) ───────────────────────── */
    --rho-widget-bg:    #0D0E1D;   /* widget ground */
    --rho-agent-bubble: #1A1B2A;
    --rho-user-bubble:  #4A4B6A;   /* tinted per [data-rho-surface] below */
    --rho-accent:       #2A8FA8;   /* shared teal — labels, subtitle, waveform */
    --rho-send:         #5B3FA8;   /* purple send — tinted per surface */
    --rho-text:         #FFFFFF;
    --rho-subtitle:     #AAAAAA;
    --rho-border:       rgba(255,255,255,0.08);
    --rho-font-display: 'rokkitt', Georgia, serif;
    --rho-font-body:    'lato', system-ui, -apple-system, 'Segoe UI', sans-serif;
    --rho-font-brand:   'cooper-black-std', 'rokkitt', Georgia, serif;  /* the "Rho" word */
    --rho-font-mono:    'droid-sans-mono', ui-monospace, 'Cascadia Mono', Consolas, monospace;

    /* chrome accent = the host-surface tint (drives glow / user bubble / on-states).
       The plasma trio + orb body stay fixed — that is Rho's identity. */
    --rho-a: ${ACCENT};
    --rho-a2: ${CFG.accent2 || '#7B3FD4'};  /* orb identity: violet */
    --rho-a3: ${hueShift(ACCENT, -28, 0.05, 0.10)};

    /* ---- RHOBEAR LIQUID GLASS — the material system -----------------------
       Derived from the Adobe Stock plasma-composer refs (2032541309 light-glass
       body + 2040938303 neon-edge composer). Three parts make the material:
         1. BODY  — a dark frosted pane you can see depth through (never a flat fill)
         2. RIM   — a specular highlight where light catches the top edge
         3. EDGE  — the cyan→violet→magenta plasma stroke (Rho's identity)
       The edge stays plasma in every app; only the ambient glow takes the app
       accent. That's what keeps Rho recognizable across the ecosystem. */
    /* The plasma trio is FIXED — it is Rho's identity and must read the same in
       every app. Binding the mid-stop to the app accent was wrong: on Captur'd
       (amber) it rendered cyan→orange→magenta, which muddies. The app accent
       lives in the bloom/glow only, never in the stroke. */
    --glass-c1: #00dfff;              /* cyan    — sampled from the ref stroke */
    --glass-c2: #7c5cff;              /* violet  — the plasma mid-stop */
    --glass-c3: #d83fff;              /* magenta — sampled from the ref stroke */
    --glass-body: linear-gradient(168deg, rgba(255,255,255,.058), rgba(255,255,255,.022) 52%, rgba(255,255,255,.04));
    --glass-blur: blur(22px) saturate(1.4);
    --glass-rim: inset 0 1px 0 rgba(255,255,255,.16), inset 0 -1px 0 rgba(255,255,255,.04);
    --glass-lift: 0 10px 34px rgba(0,0,0,.42);
    --glass-edge-w: 1.1px;            /* the plasma stroke weight */
    --glass-ease: cubic-bezier(.4,0,.2,1);

    --orb-idle: url('${ORB_BASE}/idle.png');
    --orb-thinking: url('${ORB_BASE}/thinking.png');
    --orb-speaking: url('${ORB_BASE}/speaking.png');
    --orb-error: url('${ORB_BASE}/error.png');
    --orb-loading: url('${ORB_BASE}/loading.png');
    position: fixed; z-index: 2147483000;
    font-family: var(--rho-font-body);
  }

  /* ── surface tint (rho.css [data-rho-surface] map) — chrome only, never the orb ── */
  #rho-embed[data-rho-surface="hub"],
  #rho-embed[data-rho-surface="builds"]   { --rho-user-bubble: #1E3A4A; --rho-send: #2A8FA8; }
  #rho-embed[data-rho-surface="plans"]   { --rho-user-bubble: #3A1A35; --rho-send: #C84BAA; }
  #rho-embed[data-rho-surface="designs"] { --rho-user-bubble: #3A1A1A; --rho-send: #C84B4B; }
  #rho-embed[data-rho-surface="capturd"] { --rho-user-bubble: #1A2A3A; --rho-send: #4B7AC8; }
  #rho-embed[data-rho-surface="reviews"] { --rho-user-bubble: #3A2A10; --rho-send: #D4A843; }
  #rho-embed[data-rho-surface="sales"]   { --rho-user-bubble: #2A3010; --rho-send: #8FA82A; }
  #rho-embed[data-rho-surface="lab"]     { --rho-user-bubble: #241540; --rho-send: #6B2FA8; }

  /* ---- launcher: the living orb (no glyph \u2014 the orb IS the brand) ---- */
  #rho-launch {
    position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;
    width: 60px; height: 60px; border-radius: 50%; border: none; cursor: pointer;
    padding: 0; background: transparent; touch-action: none;
    transition: transform .18s cubic-bezier(.34,1.56,.64,1);
    animation: rho-float 4.6s ease-in-out infinite;
  }
  #rho-launch.rho-dragging { animation: none !important; cursor: grabbing; transition: none !important; }
  #rho-launch:hover { transform: scale(1.08); }
  #rho-launch:active { transform: scale(.93); }
  #rho-launch.rho-hidden { display: none; }
  /* premium photoreal plasma orb — one <span> per mount, state driven by data-state */
  .rho-orbimg {
    position: absolute; inset: 0; border-radius: 50%; pointer-events: none;
    background-image: var(--orb-idle);
    background-size: cover; background-position: center; background-repeat: no-repeat;
    box-shadow: 0 8px 26px rgba(0,0,0,.45), 0 0 22px color-mix(in srgb, var(--rho-a) 40%, transparent);
    transition: box-shadow .3s ease, transform .12s ease, filter .3s ease, background-image .22s ease;
    will-change: transform, box-shadow;
  }
  .rho-orbimg[data-state="idle"]     { background-image: var(--orb-idle);     animation: rho-orb-idle 2.9s ease-in-out infinite; }
  .rho-orbimg[data-state="thinking"] { background-image: var(--orb-thinking); box-shadow: 0 8px 26px rgba(0,0,0,.45), 0 0 30px rgba(123,63,212,.6); animation: rho-orb-think .95s ease-in-out infinite; }
  .rho-orbimg[data-state="speaking"] { background-image: var(--orb-speaking); box-shadow: 0 8px 26px rgba(0,0,0,.45), 0 0 46px rgba(74,158,255,.72); animation: rho-orb-speak .5s ease-in-out infinite; }
  .rho-orbimg[data-state="error"]    { background-image: var(--orb-error);    box-shadow: 0 8px 26px rgba(0,0,0,.45), 0 0 26px rgba(255,58,58,.62); animation: rho-orb-shake .5s ease-out 1; }
  .rho-orbimg[data-state="loading"]  { background-image: var(--orb-loading);  animation: rho-orb-load 1.7s ease-in-out infinite; }
  @keyframes rho-orb-idle  { 0%,100% { transform: scale(1); }   50% { transform: scale(1.045); } }
  @keyframes rho-orb-think { 0%,100% { transform: scale(.975); } 50% { transform: scale(1.035); } }
  @keyframes rho-orb-speak { 0%,100% { transform: scale(1); }   50% { transform: scale(1.075); } }
  @keyframes rho-orb-shake { 0%,100% { transform: translateX(0); } 20% { transform: translateX(-5px); } 40% { transform: translateX(5px); } 60% { transform: translateX(-3px); } 80% { transform: translateX(3px); } }
  @keyframes rho-orb-load  { 0%,100% { opacity: .6; } 50% { opacity: .92; } }
  @keyframes rho-float { 0%,100% { translate: 0 0; } 50% { translate: 0 -5px; } }
  @media (prefers-reduced-motion: reduce) {
    #rho-launch, .rho-orbimg { animation: none !important; }
  }

  /* ---- panel: glass with a living accent aura ---- */
  #rho-panel {
    position: fixed; right: 20px; bottom: 20px; z-index: 2147483001;
    width: min(400px, calc(100vw - 32px));
    height: min(640px, calc(100vh - 40px));
    display: none; flex-direction: column; overflow: hidden;
    border-radius: 20px;
    background: var(--rho-widget-bg);
    backdrop-filter: blur(24px) saturate(1.25); -webkit-backdrop-filter: blur(24px) saturate(1.25);
    border: 1px solid rgba(255,255,255,.11);
    box-shadow: 0 26px 80px rgba(0,0,0,.62), 0 0 44px color-mix(in srgb, var(--rho-a) 20%, transparent);
    color: #fff; isolation: isolate;
    transition: width .38s cubic-bezier(.4,0,.2,1), height .38s cubic-bezier(.4,0,.2,1),
      left .38s cubic-bezier(.4,0,.2,1), top .38s cubic-bezier(.4,0,.2,1),
      right .38s cubic-bezier(.4,0,.2,1), bottom .38s cubic-bezier(.4,0,.2,1), border-radius .38s ease;
  }
  #rho-panel.rho-dragging { transition: none !important; user-select: none; }
  /* double-click the header orb → expand to a full-viewport surface */
  #rho-embed.rho-expanded #rho-panel { width: 100vw; height: 100vh; height: 100dvh; left: 0 !important; top: 0 !important; right: 0 !important; bottom: 0 !important; border-radius: 0; }
  #rho-embed.rho-expanded #rho-thread { max-width: 860px; width: 100%; margin: 0 auto; padding-left: 20px; padding-right: 20px; }
  #rho-embed.rho-expanded #rho-barwrap { max-width: 860px; width: 100%; margin: 0 auto; }
  /* expanded = full-viewport hero: big centered breathing orb + ambient glow (matches the mock) */
  #rho-embed.rho-expanded #rho-head { flex-direction: column; align-items: center; justify-content: center; gap: 14px; padding: min(9vh,88px) 20px 20px; border-bottom: none; }
  #rho-embed.rho-expanded .rho-head-orb { width: 116px; height: 116px; }
  #rho-embed.rho-expanded .rho-name { font-size: 30px; letter-spacing: .4px; }
  #rho-embed.rho-expanded .rho-spacer { display: none; }
  #rho-embed.rho-expanded #rho-call-hdr { position: absolute; top: 18px; right: 98px; }
  #rho-embed.rho-expanded #rho-expand { position: absolute; top: 18px; right: 58px; }
  #rho-embed.rho-expanded #rho-close { position: absolute; top: 18px; right: 18px; }
  #rho-embed.rho-expanded .rho-headtext { align-items: center; text-align: center; }
  #rho-embed.rho-expanded #rho-panel::before { inset: -8% -10% auto -10%; height: 62%; filter: blur(58px); opacity: 1; }
  #rho-panel::before {
    content: ""; position: absolute; inset: -30% -20% auto -20%; height: 70%; z-index: -1;
    background:
      radial-gradient(ellipse at 30% 20%, color-mix(in srgb, var(--rho-a) 34%, transparent), transparent 62%),
      radial-gradient(ellipse at 75% 35%, color-mix(in srgb, var(--rho-a2) 26%, transparent), transparent 58%);
    filter: blur(30px); pointer-events: none;
    animation: rho-aura 11s ease-in-out infinite alternate;
  }
  @keyframes rho-aura { from { transform: translate(-3%,0) scale(1); opacity:.8; } to { transform: translate(3%,4%) scale(1.08); opacity:1; } }
  #rho-embed.rho-open #rho-panel { display: flex; animation: rho-rise .26s cubic-bezier(.21,1.02,.55,1); }
  @keyframes rho-rise { from { opacity: 0; transform: translateY(14px) scale(.97); } to { opacity: 1; transform: none; } }

  #rho-head { display: flex; align-items: center; gap: 12px; padding: 12px 14px; flex-shrink: 0; border-bottom: 1px solid var(--rho-border); position: relative; cursor: grab; touch-action: none; }
  #rho-head.rho-dragging { cursor: grabbing; }
  .rho-head-orb { position: relative; width: 40px; height: 40px; border-radius: 50%; flex-shrink: 0; }
  .rho-head-orb .rho-orbrim { animation-duration: 9s; }
  #rho-embed.rho-busy .rho-head-orb { animation: rho-pulse 1.1s ease-in-out infinite; }
  @keyframes rho-pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.12); } }
  #rho-head .rho-headtext { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
  #rho-head .rho-name { font-family: var(--rho-font-brand); font-weight: 400; font-size: 20px; line-height: 1.05; letter-spacing: .2px; color: var(--rho-text); }
  #rho-head .rho-sub { font-family: var(--rho-font-body); font-size: 12.5px; line-height: 1.1; color: var(--rho-accent); }
  #rho-head .rho-sub b { font-weight: 700; }
  #rho-head .rho-chip {
    font-family: var(--rho-font-mono);
    font-size: 9.5px; font-weight: 700; letter-spacing: 1px; text-transform: uppercase;
    padding: 3px 8px; border-radius: 9px;
    background: color-mix(in srgb, var(--rho-accent) 16%, transparent);
    border: 1px solid color-mix(in srgb, var(--rho-accent) 34%, transparent); color: color-mix(in srgb, var(--rho-accent) 70%, #fff);
    animation: rho-chipglow 2.6s ease-in-out infinite;
  }
  @keyframes rho-chipglow { 0%,100% { box-shadow: 0 0 0 transparent; } 50% { box-shadow: 0 0 12px color-mix(in srgb, var(--rho-accent) 34%, transparent); } }
  #rho-head .rho-spacer { flex: 1; }
  .rho-hbtn {
    width: 30px; height: 30px; border: 1px solid rgba(255,255,255,.11); border-radius: 10px; cursor: pointer;
    background: linear-gradient(160deg, rgba(255,255,255,.085), rgba(255,255,255,.022));
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.18);
    color: rgba(255,255,255,.78); display: flex; align-items: center; justify-content: center;
    transition: transform .15s, box-shadow .15s, color .15s;
  }
  .rho-hbtn:hover { color: #fff; transform: translateY(-1px); box-shadow: 0 4px 14px rgba(0,0,0,.35); }
  .rho-hbtn svg { width: 14px; height: 14px; }

  /* ---- settings popover: colors + voices ---- */
  #rho-settings {
    display: none; position: absolute; top: 52px; right: 12px; z-index: 5;
    width: 240px; padding: 14px; border-radius: 16px;
    background: var(--glass-body), rgba(13,11,24,.82);
    backdrop-filter: blur(28px) saturate(1.5); -webkit-backdrop-filter: blur(28px) saturate(1.5);
    border: 1px solid rgba(255,255,255,.13);
    box-shadow: var(--glass-rim), 0 18px 50px rgba(0,0,0,.55);
    animation: rho-rise .2s cubic-bezier(.21,1.02,.55,1);
  }
  #rho-settings.rho-on { display: block; }
  .rho-set-label { font-family: var(--rho-font-mono); font-size: 10px; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; color: var(--rho-accent); margin: 0 0 8px; }
  .rho-set-label + .rho-set-label { margin-top: 14px; }
  .rho-swatches { display: flex; gap: 8px; flex-wrap: wrap; }
  .rho-swatch {
    width: 26px; height: 26px; border-radius: 50%; cursor: pointer; border: 2px solid transparent;
    transition: transform .12s, border-color .12s;
  }
  .rho-swatch:hover { transform: scale(1.12); }
  .rho-swatch.rho-on { border-color: #fff; box-shadow: 0 0 10px color-mix(in srgb, var(--rho-a) 60%, transparent); }
  .rho-voices { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }
  .rho-voice {
    padding: 5px 10px; border-radius: 10px; cursor: pointer; font-size: 12px; font-weight: 600;
    background: linear-gradient(160deg, rgba(255,255,255,.08), rgba(255,255,255,.025));
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.14);
    border: 1px solid rgba(255,255,255,.1); color: rgba(255,255,255,.8);
    transition: background .12s, border-color .12s, color .12s;
  }
  .rho-voice:hover { color: #fff; background: rgba(255,255,255,.1); }
  .rho-voice.rho-on { background: linear-gradient(135deg, var(--rho-a), var(--rho-a2)); border-color: transparent; color: #fff; }

  /* ---- thread + bubbles + crew chips ---- */
  #rho-thread { flex: 1; overflow-y: auto; padding: 18px 14px; display: flex; flex-direction: column; gap: 11px; }
  #rho-thread::-webkit-scrollbar { width: 7px; }
  #rho-thread::-webkit-scrollbar-thumb { background: rgba(255,255,255,.14); border-radius: 4px; }
  .rho-msg {
    position: relative; max-width: 86%; padding: 10px 14px; border-radius: 17px;
    font-size: 14.5px; line-height: 1.48; white-space: pre-wrap; word-wrap: break-word;
    animation: rho-msgin .3s cubic-bezier(.21,1.02,.55,1);
  }
  @keyframes rho-msgin { from { opacity: 0; transform: translateY(8px) scale(.97); } to { opacity: 1; transform: none; } }
  /* user bubble — accent-tinted glass, not a solid slab. The plasma reads as
     light held INSIDE the pane rather than paint sprayed on top of it. */
  .rho-msg.user {
    align-self: flex-end; color: #fff; border-bottom-right-radius: 6px;
    background:
      linear-gradient(120deg, color-mix(in srgb, var(--rho-a) 46%, transparent), color-mix(in srgb, var(--rho-a2) 38%, transparent) 62%, color-mix(in srgb, var(--rho-a2) 22%, transparent)),
      rgba(9,10,12,.34);
    backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
    border: 1px solid color-mix(in srgb, var(--rho-a) 30%, rgba(255,255,255,.14));
    box-shadow: 0 6px 22px color-mix(in srgb, var(--rho-a) 26%, transparent), var(--glass-rim);
  }
  .rho-msg.user.rho-sendoff { animation: rho-msgin .3s cubic-bezier(.21,1.02,.55,1), rho-glowoff 1.1s ease-out; }
  @keyframes rho-glowoff {
    0% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--rho-a) 55%, transparent), 0 6px 20px color-mix(in srgb, var(--rho-a) 32%, transparent); }
    100% { box-shadow: 0 6px 20px color-mix(in srgb, var(--rho-a) 32%, transparent), inset 0 1px 0 rgba(255,255,255,.32); }
  }
  .rho-msg.assistant {
    align-self: flex-start; color: rgba(255,255,255,.95); border-bottom-left-radius: 6px;
    background: var(--glass-body), rgba(9,10,12,.42);
    backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
    border: 1px solid rgba(255,255,255,.1);
    box-shadow: var(--glass-rim), 0 6px 22px rgba(0,0,0,.32);
    overflow: hidden;
  }
  .rho-msg.assistant::before {
    content: ""; position: absolute; top: 0; left: 8%; right: 8%; height: 1px;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.5), transparent);
  }
  .rho-msg.assistant.is-streaming:empty { min-width: 54px; min-height: 20px; }
  .rho-msg.assistant.is-streaming:empty::after {
    content: "\\00B7 \\00B7 \\00B7"; font-weight: 800; letter-spacing: 2px;
    background: linear-gradient(90deg, var(--rho-a), var(--rho-a2));
    -webkit-background-clip: text; background-clip: text; color: transparent;
    animation: rho-dots 1.1s ease-in-out infinite;
  }
  .rho-msg.assistant.is-streaming:not(:empty)::after { content: '\\258B'; opacity: .5; animation: rho-blink 1s steps(2) infinite; }
  @keyframes rho-dots { 0%,100% { opacity: .35; } 50% { opacity: 1; } }
  @keyframes rho-blink { 50% { opacity: 0; } }

  /* per-message actions: copy the whole block + Listen (TTS speaks it aloud) */
  .rho-msg-actions {
    align-self: flex-start; display: flex; gap: 4px; margin: -4px 0 2px 4px;
    opacity: .5; transition: opacity .18s;
  }
  .rho-msg-actions:hover, .rho-msg-act.rho-on { opacity: 1; }
  .rho-msg-act {
    display: inline-flex; align-items: center; gap: 5px; padding: 4px 9px; border-radius: 9px;
    border: 1px solid rgba(255,255,255,.1);
    background: linear-gradient(160deg, rgba(255,255,255,.07), rgba(255,255,255,.02));
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.14);
    color: rgba(255,255,255,.66); font-size: 11px; font-weight: 600; cursor: pointer;
    font-family: inherit; transition: color .15s, background .15s, border-color .15s;
  }
  .rho-msg-act:hover { color: #fff; background: rgba(255,255,255,.1); }
  .rho-msg-act svg { width: 13px; height: 13px; }
  .rho-msg-act.rho-on { color: #fff; border-color: transparent; background: linear-gradient(135deg, var(--rho-a), var(--rho-a2)); box-shadow: 0 0 12px color-mix(in srgb, var(--rho-a) 40%, transparent); }
  .rho-msg-act.rho-ok { color: #43c98a; border-color: rgba(67,201,138,.4); }

  /* the send comet: a spark that flies from the bar up to the head orb */
  .rho-comet {
    position: absolute; width: 10px; height: 10px; border-radius: 50%; z-index: 6; pointer-events: none;
    background: radial-gradient(circle, #fff, var(--rho-a) 55%, transparent 75%);
    box-shadow: 0 0 14px color-mix(in srgb, var(--rho-a) 80%, transparent);
  }

  /* crew chips \u2014 live tool/agent activity, Claude-Code style */
  .rho-crew { align-self: flex-start; display: flex; flex-direction: column; gap: 6px; max-width: 92%; }
  .rho-chip-tool {
    position: relative; display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 12px; border-radius: 12px;
    font-size: 12px; font-weight: 600; letter-spacing: .2px;
    font-family: var(--rho-font-mono);
    background: var(--glass-body), rgba(9,10,12,.4);
    backdrop-filter: blur(16px) saturate(1.3); -webkit-backdrop-filter: blur(16px) saturate(1.3);
    border: 1px solid rgba(255,255,255,.1);
    box-shadow: var(--glass-rim), 0 4px 14px rgba(0,0,0,.28);
    color: rgba(255,255,255,.8);
    animation: rho-msgin .25s cubic-bezier(.21,1.02,.55,1);
  }
  .rho-chip-tool .rho-dot {
    width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
    background: linear-gradient(135deg, var(--rho-a), var(--rho-a2));
    box-shadow: 0 0 8px color-mix(in srgb, var(--rho-a) 70%, transparent);
    animation: rho-dotpulse 1s ease-in-out infinite;
  }
  @keyframes rho-dotpulse { 0%,100% { transform: scale(1); opacity: 1; } 50% { transform: scale(.6); opacity: .5; } }
  .rho-chip-tool.rho-done { color: rgba(255,255,255,.45); }
  .rho-chip-tool.rho-done .rho-dot { animation: none; background: #43c98a; box-shadow: none; }
  .rho-chip-tool .rho-chip-task { color: rgba(255,255,255,.5); font-weight: 400; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px; }

  .rho-attach-pill {
    align-self: flex-end; display: inline-flex; align-items: center; gap: 8px;
    padding: 5px 10px; border-radius: 10px; font-size: 11.5px; font-weight: 600;
    background: var(--glass-body), rgba(9,10,12,.4);
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    border: 1px solid rgba(255,255,255,.12); color: rgba(255,255,255,.78);
    box-shadow: var(--glass-rim);
  }
  .rho-attach-pill img { width: 26px; height: 26px; border-radius: 6px; object-fit: cover; }
  .rho-attach-pill button { border: none; background: transparent; color: rgba(255,255,255,.6); cursor: pointer; font-size: 13px; padding: 0 2px; }

  /* ---- LIQUID GLASS primitives -------------------------------------------
     .rho-glass  = frosted body + specular rim + lift
     .rho-edge   = the plasma stroke, painted as a masked gradient ring so the
                   colour can run around the whole radius (a 1px border can't).
     Compose them: class="rho-glass rho-edge". Any element that reads as a
     surface (bar, bubble, chip, button, popover) uses these — never a flat fill. */
  .rho-glass {
    position: relative;
    background: var(--glass-body);
    backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
    box-shadow: var(--glass-rim), var(--glass-lift);
  }
  .rho-edge::after {
    content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
    padding: var(--glass-edge-w);
    background: conic-gradient(from var(--rho-edge-angle) at 50% 50%,
      var(--glass-c1), var(--glass-c2) 28%, var(--glass-c3) 52%, var(--glass-c2) 76%, var(--glass-c1));
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor;
            mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
            mask-composite: exclude;
    opacity: .5; transition: opacity .28s var(--glass-ease);
  }
  /* the specular sheen — light catching the top curve of the pane */
  .rho-sheen::before {
    content: ""; position: absolute; top: 0; left: 9%; right: 9%; height: 1px; pointer-events: none;
    background: linear-gradient(90deg, transparent, rgba(255,255,255,.66), transparent);
  }

  /* ---- the Adobe chat bus ---- */
  #rho-barwrap { padding: 10px 14px calc(14px + env(safe-area-inset-bottom, 0px)); flex-shrink: 0; position: relative; }
  #rho-bar {
    position: relative; border-radius: 24px; padding: 14px 14px 9px;
    background:
      linear-gradient(168deg, rgba(255,255,255,.05), rgba(255,255,255,.018) 55%, rgba(255,255,255,.035)),
      rgba(9,10,12,.55);
    backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
    box-shadow: var(--glass-rim), var(--glass-lift);
    transition: box-shadow .28s var(--glass-ease);
  }
  #rho-bar::after {
    content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
    padding: var(--glass-edge-w);
    background: conic-gradient(from var(--rho-edge-angle) at 50% 50%,
      var(--glass-c1), var(--glass-c2) 28%, var(--glass-c3) 52%, var(--glass-c2) 76%, var(--glass-c1));
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor;
            mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
            mask-composite: exclude;
    opacity: .45; transition: opacity .28s var(--glass-ease);
  }
  #rho-bar::before { content: ""; position: absolute; top: 0; left: 10%; right: 10%; height: 1px; background: linear-gradient(90deg, transparent, rgba(255,255,255,.72), transparent); }
  /* focus doesn't recolour the edge — it makes the plasma burn brighter + blooms */
  #rho-bar:focus-within { box-shadow: var(--glass-rim), 0 10px 34px rgba(0,0,0,.42), 0 0 30px color-mix(in srgb, var(--rho-a) 22%, transparent); }
  #rho-bar:focus-within::after { opacity: 1; }
  @media (prefers-reduced-motion: no-preference) {
    #rho-bar:focus-within::after { animation: rho-edgeflow 7s linear infinite; }
  }
  /* the plasma slowly travels the stroke while the user is composing.
     Gradients can't tween, so the angle is a registered custom property —
     that's what makes it animatable. Where @property is unsupported the
     angle just holds at 200deg and the edge stays static (still correct). */
  @keyframes rho-edgeflow { to { --rho-edge-angle: 560deg; } }
  #rho-input { width: 100%; border: none; background: transparent; color: #fff; font-size: 15px; line-height: 1.45; resize: none; outline: none; min-height: 22px; max-height: 120px; padding: 0 2px; font-family: inherit; }
  #rho-input::placeholder { color: rgba(255,255,255,.46); font-weight: 500; }
  .rho-barrow { display: flex; align-items: center; gap: 4px; margin-top: 9px; }
  .rho-barrow .grow { flex: 1; }
  .rho-bbtn {
    position: relative;
    width: 34px; height: 34px; border-radius: 50%; cursor: pointer; flex-shrink: 0;
    border: 1px solid rgba(255,255,255,.11);
    background: linear-gradient(160deg, rgba(255,255,255,.085), rgba(255,255,255,.022));
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.18);
    color: rgba(255,255,255,.78); display: flex; align-items: center; justify-content: center;
    transition: transform .15s, box-shadow .15s, color .15s;
  }
  .rho-bbtn:hover { color: #fff; transform: translateY(-1px); box-shadow: 0 4px 14px color-mix(in srgb, var(--rho-a) 24%, transparent); }
  .rho-bbtn svg { width: 18px; height: 18px; }
  .rho-bbtn.rho-on { background: linear-gradient(135deg, var(--rho-a3), var(--rho-a)); color: #fff; border-color: transparent; box-shadow: 0 0 16px color-mix(in srgb, var(--rho-a) 45%, transparent); }
  #rho-think.rho-on { background: linear-gradient(135deg, #2b2350, var(--rho-a)); }
  #rho-call { color: rgba(255,255,255,.85); }
  /* send — the pack's purple key (rho.css --rho-send), rounded-square, clean
     white arrow. The one solid-colour control; everything else is glass. */
  #rho-send {
    position: relative;
    width: 40px; height: 40px; border-radius: 12px; border: none; margin-left: 4px; color: #fff; cursor: pointer; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    background: var(--rho-send);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.28), 0 4px 16px color-mix(in srgb, var(--rho-send) 40%, transparent);
    transition: transform .12s, box-shadow .2s, opacity .2s, filter .2s;
  }
  #rho-send:hover:not(:disabled) { transform: translateY(-1px); filter: brightness(1.08); box-shadow: inset 0 1px 0 rgba(255,255,255,.28), 0 6px 22px color-mix(in srgb, var(--rho-send) 55%, transparent); }
  #rho-send:active { transform: scale(.92); }
  #rho-send:disabled { opacity: .4; cursor: not-allowed; }
  #rho-send svg { width: 17px; height: 17px; }
  .rho-hint { font-size: 10px; color: rgba(255,255,255,.30); text-align: center; margin: 6px 0 0; }

  /* ---- plus menu ---- */
  #rho-plusmenu {
    display: none; position: absolute; bottom: 66px; left: 18px; z-index: 6;
    min-width: 190px; padding: 6px; border-radius: 14px;
    background: rgba(16,14,30,.97); border: 1px solid rgba(255,255,255,.12);
    box-shadow: 0 18px 50px rgba(0,0,0,.55);
    animation: rho-rise .18s cubic-bezier(.21,1.02,.55,1);
  }
  #rho-plusmenu.rho-on { display: block; }
  .rho-pmitem {
    display: flex; align-items: center; gap: 10px; width: 100%;
    padding: 9px 10px; border: none; border-radius: 9px; cursor: pointer;
    background: transparent; color: rgba(255,255,255,.85); font-size: 13.5px; font-weight: 600; text-align: left;
    font-family: inherit;
  }
  .rho-pmitem:hover { background: rgba(255,255,255,.08); color: #fff; }
  .rho-pmitem svg { width: 15px; height: 15px; opacity: .75; }

  /* ---- THE BIG ONE: fullscreen voice call ---- */
  #rho-call-surface {
    display: none; position: fixed; inset: 0; z-index: 2147483002;
    flex-direction: column; align-items: center; justify-content: center; gap: 26px;
    background:
      radial-gradient(ellipse at 50% 34%, color-mix(in srgb, var(--rho-a) 16%, transparent), transparent 60%),
      radial-gradient(ellipse at 20% 80%, color-mix(in srgb, var(--rho-a3) 10%, transparent), transparent 55%),
      rgba(6,5,14,.97);
    backdrop-filter: blur(30px); -webkit-backdrop-filter: blur(30px);
    color: #fff;
  }
  #rho-embed.rho-call-open #rho-call-surface { display: flex; animation: rho-callin .34s cubic-bezier(.21,1.02,.55,1); }
  @keyframes rho-callin { from { opacity: 0; } to { opacity: 1; } }
  #rho-call-orb {
    position: relative; width: min(46vmin, 240px); height: min(46vmin, 240px);
    border-radius: 50%; border: none; cursor: pointer; background: transparent; padding: 0;
    transition: transform .3s cubic-bezier(.34,1.56,.64,1);
  }
  #rho-call-orb .rho-orbrim { animation-duration: 8s; }
  #rho-embed.rho-v-listening #rho-call-orb { transform: scale(1.03); }
  #rho-embed.rho-v-listening #rho-call-orb .rho-orbcore { animation-duration: 2.2s, 14s; }
  #rho-embed.rho-v-thinking #rho-call-orb .rho-orbrim { animation-duration: 1.6s; }
  #rho-embed.rho-v-speaking #rho-call-orb { animation: rho-speakpulse .62s ease-in-out infinite; }
  @keyframes rho-speakpulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.055); } }
  #rho-call-name { font-family: var(--rho-font-brand); font-weight: 400; font-size: clamp(30px, 6vmin, 46px); color: var(--rho-text); line-height: 1; margin-top: -6px; }
  #rho-call-state {
    font-family: var(--rho-font-body);
    font-size: clamp(15px, 2.2vmin, 19px); font-weight: 400; letter-spacing: .3px;
    color: var(--rho-accent); min-height: 15px;
  }
  #rho-call-line {
    max-width: min(640px, 86vw); min-height: 52px; text-align: center;
    font-size: clamp(16px, 2.6vmin, 21px); line-height: 1.5; color: rgba(255,255,255,.92);
  }
  #rho-call-line .rho-heard { color: rgba(255,255,255,.5); font-style: italic; }
  #rho-call-crew { display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; max-width: 86vw; min-height: 30px; }
  #rho-call-exit {
    position: absolute; top: max(18px, env(safe-area-inset-top)); right: 22px;
    width: 42px; height: 42px; border-radius: 50%;
  }
  #rho-call-hint { position: absolute; bottom: max(20px, env(safe-area-inset-bottom)); font-size: 11.5px; color: rgba(255,255,255,.35); letter-spacing: .4px; }

  /* ── live voice waveform (teal, pack signature) — real mic RMS drives it ── */
  .rho-wave { display: none; height: 26px; align-items: center; gap: 2px; }
  .rho-wave.rho-on { display: inline-flex; }
  .rho-wave i {
    width: 3px; height: 4px; border-radius: 2px; flex-shrink: 0;
    background: var(--rho-accent);
    box-shadow: 0 0 6px color-mix(in srgb, var(--rho-accent) 60%, transparent);
    transition: height .09s ease;
  }
  /* the composer's inline waveform sits between the mic and the send */
  #rho-bar-wave { flex: 1; justify-content: center; min-width: 0; overflow: hidden; }
  /* the voice surface's full-width strip above the bottom controls */
  #rho-call-wave { height: 40px; gap: 3px; }
  #rho-call-wave i { width: 4px; }

  /* Voice follow-up stays visible without turning ordinary chat into a call.
     Stop always means "back to normal dictation" - no hidden sticky mode. */
  #rho-voice-dock {
    display: none; position: fixed; top: max(14px, env(safe-area-inset-top)); right: 16px;
    z-index: 2147483003; align-items: center; gap: 8px; padding: 8px 10px 8px 13px;
    border: 1px solid color-mix(in srgb, var(--rho-a) 55%, rgba(255,255,255,.16)); border-radius: 15px;
    background: rgba(13,12,24,.92); box-shadow: 0 12px 34px rgba(0,0,0,.38);
    color: #fff; backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
    font: 700 12px/1 var(--rho-font-body);
  }
  #rho-embed.rho-follow-on #rho-voice-dock { display: flex; }
  #rho-voice-status { max-width: 160px; color: rgba(255,255,255,.86); }
  .rho-voice-dock-btn {
    width: 36px; height: 36px; padding: 0; border: 0; border-radius: 11px; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; color: #fff;
    background: linear-gradient(135deg, var(--rho-a3), var(--rho-a));
    box-shadow: inset 0 1px 0 rgba(255,255,255,.22), 0 3px 12px color-mix(in srgb, var(--rho-a) 35%, transparent);
  }
  .rho-voice-dock-btn:hover { filter: brightness(1.12); }
  .rho-voice-dock-btn svg { width: 18px; height: 18px; }
  #rho-voice-stop { background: rgba(255,255,255,.12); box-shadow: none; }

  /* Phones: the panel is a full-screen surface, not a floating window */
  @media (max-width: 520px) {
    #rho-panel { right: 0; bottom: 0; width: 100vw; height: 100vh; height: 100dvh; border-radius: 0; }
    #rho-launch { width: 52px; height: 52px; }
  }

  html[data-theme="light"] #rho-panel, :root[data-color-scheme="light"] #rho-panel { color: #fff; }

  /* sign-in card: Rho is tied to credits, so Rho knows who you are */
  #rho-embed .rho-signin {
    margin: 10px 4px 4px; padding: 16px 16px 14px; border-radius: 16px;
    background: linear-gradient(160deg, rgba(255,255,255,.05), rgba(255,255,255,.015));
    border: 1px solid rgba(255,255,255,.09); box-shadow: 0 12px 32px rgba(0,0,0,.35);
  }
  #rho-embed .rho-signin-title { font-weight: 700; font-size: 15px; margin-bottom: 5px; color: #eef4fb; }
  #rho-embed .rho-signin-sub { font-size: 12.5px; line-height: 1.5; color: rgba(230,238,247,.62); margin-bottom: 12px; }
  #rho-embed .rho-gbtn {
    display: flex; align-items: center; justify-content: center; gap: 10px; width: 100%;
    padding: 10px 14px; border-radius: 12px; border: 1px solid rgba(255,255,255,.14);
    background: #fff; color: #1f1f1f; font-weight: 600; font-size: 13.5px; cursor: pointer;
    transition: transform .15s ease, box-shadow .15s ease;
  }
  #rho-embed .rho-gbtn:hover {
    transform: translateY(-1px);
    box-shadow: 0 8px 22px rgba(0,0,0,.35), 0 0 0 3px color-mix(in srgb, var(--rho-a) 25%, transparent);
  }
  `;
  var styleEl = document.createElement('style');
  styleEl.id = 'rho-embed-style';
  styleEl.textContent = css;

  // ---- markup -------------------------------------------------------------
  var ICON = {
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    think: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6.5 6.5 0 0 1 6.5 6.5c0 1.9-.86 3.4-2 4.5-.83.8-1.5 1.6-1.5 2.5V18h-6v-1.5c0-.9-.67-1.7-1.5-2.5-1.14-1.1-2-2.6-2-4.5A6.5 6.5 0 0 1 12 3z"/><path d="M9.5 21h5"/></svg>',
    mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>',
    call: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 12h2M7 8v8M11 5v14M15 8v8M19 10v4"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true"><path d="M3.4 20.4l17.45-8.05a.5.5 0 0 0 0-.9L3.4 3.4a.5.5 0 0 0-.7.62l2.6 6.9a.5.5 0 0 0 .38.32l8.42 1.26-8.42 1.26a.5.5 0 0 0-.38.32l-2.6 6.9a.5.5 0 0 0 .7.62z"/></svg>',
    expand: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/></svg>',
    image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5-9 9"/></svg>',
    fresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
    speaker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14"/></svg>',
    stopspk: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
    pause: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5h3v14H7zm7 0h3v14h-3z"/></svg>',
    play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="m8 5 11 7-11 7z"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
  };
  var ORB = '<span class="rho-orbimg" data-state="idle"></span>';

  var root = document.createElement('div');
  root.id = 'rho-embed';
  root.innerHTML =
    '<button id="rho-launch" aria-label="Open ' + TITLE + '">' + ORB + '</button>' +
    '<section id="rho-panel" role="dialog" aria-label="' + TITLE + ' chat">' +
      '<header id="rho-head">' +
        '<span class="rho-head-orb">' + ORB + '</span>' +
        '<span class="rho-headtext">' +
          '<span class="rho-name">' + TITLE + '</span>' +
          '<span class="rho-sub">riding <b>' + SURFACE_LABEL + '</b></span>' +
        '</span>' +
        (READY ? '' : '<span class="rho-chip" id="rho-chip">warming up</span>') +
        '<span class="rho-spacer"></span>' +
        '<button class="rho-hbtn" id="rho-call-hdr" aria-label="Talk to ' + TITLE + '" title="Talk to ' + TITLE + '">' + ICON.mic + '</button>' +
        '<button class="rho-hbtn" id="rho-expand" aria-label="Expand" title="Expand">' + ICON.expand + '</button>' +
        '<button class="rho-hbtn" id="rho-close" aria-label="Close">' + ICON.close + '</button>' +
        '<div id="rho-settings" role="menu" aria-label="Personalize ' + TITLE + '">' +
          '<p class="rho-set-label">Accent</p>' +
          '<div class="rho-swatches" id="rho-swatches"></div>' +
          '<p class="rho-set-label">Voice</p>' +
          '<div class="rho-voices" id="rho-voices"></div>' +
        '</div>' +
      '</header>' +
      '<div id="rho-thread" aria-live="polite"></div>' +
      '<div id="rho-barwrap">' +
        '<div id="rho-plusmenu" role="menu">' +
          '<button class="rho-pmitem" id="rho-pm-image">' + ICON.image + 'Attach an image</button>' +
          '<button class="rho-pmitem" id="rho-pm-new">' + ICON.fresh + 'Start fresh</button>' +
          '<button class="rho-pmitem" id="rho-pm-copy">' + ICON.copy + 'Copy conversation</button>' +
          '<button class="rho-pmitem" id="rho-pm-personalize">' + ICON.gear + 'Personalize</button>' +
        '</div>' +
        '<div id="rho-bar">' +
          '<textarea id="rho-input" rows="1" placeholder="Type your message…" aria-label="Message ' + TITLE + '"></textarea>' +
          '<div class="rho-barrow">' +
            '<button class="rho-bbtn" id="rho-plus" title="More" aria-label="More options">' + ICON.plus + '</button>' +
            '<button class="rho-bbtn" id="rho-think" title="Deep thinking" aria-label="Toggle deep thinking" aria-pressed="false">' + ICON.think + '</button>' +
            '<button class="rho-bbtn" id="rho-dictate" title="Dictate into the message" aria-label="Dictate">' + ICON.mic + '</button>' +
            '<span class="rho-wave" id="rho-bar-wave" aria-hidden="true"></span>' +
            '<span class="grow"></span>' +
            '<button class="rho-bbtn" id="rho-call" title="Talk to ' + TITLE + '" aria-label="Talk to ' + TITLE + '">' + ICON.call + '</button>' +
            '<button id="rho-send" aria-label="Send" disabled>' + ICON.send + '</button>' +
          '</div>' +
        '</div>' +
        '<p class="rho-hint">' + (READY ? 'Mic dictates. Listen beneath a reply turns on voice follow-up. The waveform opens a live call.' : TITLE + ' is being wired in \u2014 answers land here soon') + '</p>' +
      '</div>' +
    '</section>' +
    '<div id="rho-voice-dock" role="group" aria-label="Voice replies are on">' +
      '<span id="rho-voice-status">Voice replies on</span>' +
      '<button class="rho-voice-dock-btn" id="rho-voice-pause" type="button" aria-label="Pause voice playback" title="Pause voice playback">' + ICON.pause + '</button>' +
      '<button class="rho-voice-dock-btn" id="rho-voice-stop" type="button" aria-label="Stop voice replies and return to dictation" title="Stop voice replies">' + ICON.stopspk + '</button>' +
    '</div>' +
    '<div id="rho-call-surface" role="dialog" aria-label="Voice call with ' + TITLE + '">' +
      '<button class="rho-hbtn" id="rho-call-exit" aria-label="End the call">' + ICON.close + '</button>' +
      '<button id="rho-call-orb" aria-label="Tap to interrupt">' + ORB + '</button>' +
      '<div id="rho-call-name">' + TITLE + '</div>' +
      '<div id="rho-call-state">connecting</div>' +
      '<div id="rho-call-line"></div>' +
      '<div id="rho-call-crew"></div>' +
      '<span class="rho-wave" id="rho-call-wave" aria-hidden="true"></span>' +
      '<div id="rho-call-hint">Just talk \u2014 ' + TITLE + ' is listening. Tap the orb to cut in.</div>' +
    '</div>';

  function mount() {
    // Typekit sbv5bcv — the ONE RHOBEAR font contract (rokkitt / lato /
    // droid-sans-mono + cooper-black-std for the "Rho" word). NEVER Nacelle,
    // never a Google-Fonts link. Idempotent across double-mounts.
    if (!document.getElementById('rho-typekit')) {
      var tk = document.createElement('link');
      tk.id = 'rho-typekit'; tk.rel = 'stylesheet';
      tk.href = 'https://use.typekit.net/sbv5bcv.css';
      document.head.appendChild(tk);
    }
    root.setAttribute('data-rho-surface', SURFACE);
    document.head.appendChild(styleEl);
    document.body.appendChild(root);
    wire();
  }

  // ---- behavior -----------------------------------------------------------
  function wire() {
    var launch  = root.querySelector('#rho-launch');
    var closeBtn= root.querySelector('#rho-close');
    var thread  = root.querySelector('#rho-thread');
    var input   = root.querySelector('#rho-input');
    var sendBtn = root.querySelector('#rho-send');
    var dictate = root.querySelector('#rho-dictate');
    var plusBtn = root.querySelector('#rho-plus');
    var plusMenu= root.querySelector('#rho-plusmenu');
    var thinkBtn= root.querySelector('#rho-think');
    var personalizeBtn = root.querySelector('#rho-pm-personalize');
    var settings= root.querySelector('#rho-settings');
    var callBtn = root.querySelector('#rho-call');
    var callHdrBtn = root.querySelector('#rho-call-hdr');
    var expandBtn = root.querySelector('#rho-expand');
    var barWave = root.querySelector('#rho-bar-wave');
    var callWave = root.querySelector('#rho-call-wave');
    var voiceDock = root.querySelector('#rho-voice-dock');
    var voiceStatus = root.querySelector('#rho-voice-status');
    var voicePause = root.querySelector('#rho-voice-pause');
    var voiceStop = root.querySelector('#rho-voice-stop');
    var callExit= root.querySelector('#rho-call-exit');
    var callOrb = root.querySelector('#rho-call-orb');
    var callState = root.querySelector('#rho-call-state');
    var callLine  = root.querySelector('#rho-call-line');
    var callCrew  = root.querySelector('#rho-call-crew');
    var thinking = false;
    var panel = root.querySelector('#rho-panel');
    var head = root.querySelector('#rho-head');
    var positionKey = 'rho.panel.position.v1.' + SURFACE;
    var drag = null;
    var launchPositionKey = 'rho.launch.position.v1.' + SURFACE;
    var launchDrag = null;
    var suppressLaunchClick = false;

    function viewportPosition(pos) {
      var rect = panel.getBoundingClientRect();
      var width = rect.width || Math.min(400, Math.max(0, window.innerWidth - 32));
      var height = rect.height || Math.min(640, Math.max(0, window.innerHeight - 40));
      var maxLeft = Math.max(8, window.innerWidth - width - 8);
      var maxTop = Math.max(8, window.innerHeight - height - 8);
      return {
        left: Math.max(8, Math.min(maxLeft, Number(pos.left) || 0)),
        top: Math.max(8, Math.min(maxTop, Number(pos.top) || 0))
      };
    }
    function setPanelPosition(pos, persist) {
      var safe = viewportPosition(pos);
      panel.style.left = safe.left + 'px';
      panel.style.top = safe.top + 'px';
      panel.style.right = 'auto';
      panel.style.bottom = 'auto';
      if (persist) lsSet(positionKey, JSON.stringify(safe));
    }
    function restorePanelPosition() {
      var raw = lsGet(positionKey);
      if (!raw) return;
      try {
        var pos = JSON.parse(raw);
        if (pos && Number.isFinite(Number(pos.left)) && Number.isFinite(Number(pos.top))) setPanelPosition(pos, false);
      } catch (e) {}
    }
    function clampPanelPosition() {
      if (!panel.style.left || root.classList.contains('rho-expanded')) return;
      setPanelPosition({ left: parseFloat(panel.style.left), top: parseFloat(panel.style.top) }, true);
    }
    function dragExcluded(target) {
      for (var node = target; node && node !== head; node = node.parentNode) {
        var tag = (node.tagName || '').toLowerCase();
        if (tag === 'button' || tag === 'input' || tag === 'textarea' || tag === 'select' || tag === 'a') return true;
        if (node.classList && node.classList.contains('rho-head-orb')) return true;
      }
      return false;
    }
    function onDragStart(e) {
      if (root.classList.contains('rho-expanded') || drag || dragExcluded(e.target)) return;
      var rect = panel.getBoundingClientRect();
      drag = { id: e.pointerId, dx: e.clientX - rect.left, dy: e.clientY - rect.top };
      panel.classList.add('rho-dragging');
      head.classList.add('rho-dragging');
      if (head.setPointerCapture) head.setPointerCapture(e.pointerId);
      e.preventDefault();
    }
    function onDragMove(e) {
      if (!drag || e.pointerId !== drag.id) return;
      setPanelPosition({ left: e.clientX - drag.dx, top: e.clientY - drag.dy }, false);
      e.preventDefault();
    }
    function onDragEnd(e) {
      if (!drag || (e.pointerId != null && e.pointerId !== drag.id)) return;
      var left = parseFloat(panel.style.left), top = parseFloat(panel.style.top);
      if (Number.isFinite(left) && Number.isFinite(top)) setPanelPosition({ left: left, top: top }, true);
      if (head.releasePointerCapture && e.pointerId != null) {
        try { head.releasePointerCapture(e.pointerId); } catch (err) {}
      }
      drag = null;
      panel.classList.remove('rho-dragging');
      head.classList.remove('rho-dragging');
    }

    function viewportLaunchPosition(pos) {
      var rect = launch.getBoundingClientRect();
      var width = rect.width || 76;
      var height = rect.height || 76;
      var maxLeft = Math.max(8, window.innerWidth - width - 8);
      var maxTop = Math.max(8, window.innerHeight - height - 8);
      return {
        left: Math.max(8, Math.min(maxLeft, Number(pos.left) || 0)),
        top: Math.max(8, Math.min(maxTop, Number(pos.top) || 0))
      };
    }
    function setLaunchPosition(pos, persist) {
      var safe = viewportLaunchPosition(pos);
      launch.style.left = safe.left + 'px';
      launch.style.top = safe.top + 'px';
      launch.style.right = 'auto';
      launch.style.bottom = 'auto';
      if (persist) lsSet(launchPositionKey, JSON.stringify(safe));
    }
    function restoreLaunchPosition() {
      var raw = lsGet(launchPositionKey);
      if (!raw) return;
      try {
        var pos = JSON.parse(raw);
        if (pos && Number.isFinite(Number(pos.left)) && Number.isFinite(Number(pos.top))) setLaunchPosition(pos, false);
      } catch (e) {}
    }
    function clampLaunchPosition() {
      if (!launch.style.left || root.classList.contains('rho-open')) return;
      setLaunchPosition({ left: parseFloat(launch.style.left), top: parseFloat(launch.style.top) }, true);
    }
    function onLaunchPointerDown(e) {
      if (launchDrag) return;
      var rect = launch.getBoundingClientRect();
      launchDrag = { id: e.pointerId, startX: e.clientX, startY: e.clientY, dx: e.clientX - rect.left, dy: e.clientY - rect.top, moved: false };
      launch.classList.add('rho-dragging');
      if (launch.setPointerCapture) launch.setPointerCapture(e.pointerId);
      e.preventDefault();
    }
    function onLaunchPointerMove(e) {
      if (!launchDrag || e.pointerId !== launchDrag.id) return;
      if (!launchDrag.moved && Math.hypot(e.clientX - launchDrag.startX, e.clientY - launchDrag.startY) > 5) launchDrag.moved = true;
      if (launchDrag.moved) {
        setLaunchPosition({ left: e.clientX - launchDrag.dx, top: e.clientY - launchDrag.dy }, false);
        e.preventDefault();
      }
    }
    function onLaunchPointerEnd(e) {
      if (!launchDrag || (e.pointerId != null && e.pointerId !== launchDrag.id)) return;
      if (launchDrag.moved) {
        setLaunchPosition({ left: parseFloat(launch.style.left), top: parseFloat(launch.style.top) }, true);
        suppressLaunchClick = true;
      }
      if (launch.releasePointerCapture && e.pointerId != null) {
        try { launch.releasePointerCapture(e.pointerId); } catch (err) {}
      }
      launchDrag = null;
      launch.classList.remove('rho-dragging');
    }

    function open() {
      root.classList.add('rho-open');
      launch.classList.add('rho-hidden');
      restorePanelPosition();
      if (!greeted) { greeted = true; attachActions(append('assistant', GREETING)); }
      checkAuth();
      setTimeout(function () { input.focus(); }, 260);
    }
    function close() {
      root.classList.remove('rho-open');
      launch.classList.remove('rho-hidden');
      settings.classList.remove('rho-on');
      plusMenu.classList.remove('rho-on');
      stopMsgSpeak();
      exitVoiceFollow();
    }

    restoreLaunchPosition();
    launch.addEventListener('pointerdown', onLaunchPointerDown);
    launch.addEventListener('pointermove', onLaunchPointerMove);
    launch.addEventListener('pointerup', onLaunchPointerEnd);
    launch.addEventListener('pointercancel', onLaunchPointerEnd);
    launch.addEventListener('click', function () {
      if (suppressLaunchClick) { suppressLaunchClick = false; return; }
      open();
    });
    closeBtn.addEventListener('click', close);

    // Rho is a movable companion, not a fixed obstruction. Drag the header
    // chrome anywhere in the viewport; buttons and the expand orb retain
    // their click/double-click contracts. The position is remembered per
    // surface so a useful placement in Builds does not hijack Plans.
    head.setAttribute('title', 'Drag to move Rho');
    head.addEventListener('pointerdown', onDragStart);
    head.addEventListener('pointermove', onDragMove);
    head.addEventListener('pointerup', onDragEnd);
    head.addEventListener('pointercancel', onDragEnd);
    window.addEventListener('resize', clampPanelPosition);
    window.addEventListener('resize', clampLaunchPosition);

    // double-click the header orb → expand the panel to a full-viewport surface
    var headOrb = root.querySelector('.rho-head-orb');
    if (headOrb) {
      headOrb.style.cursor = 'pointer';
      headOrb.title = 'Double-click to expand';
      headOrb.addEventListener('dblclick', function () { root.classList.toggle('rho-expanded'); });
    }

    // ---- sign-in: Rho spends credits, so Rho knows who you are --------------
    var signinCard = null;
    function centralAuthResult() {
      if (window.__user && window.__user.email) {
        return Promise.resolve({ ok: true, status: 200, data: { user: window.__user } });
      }
      if (USE_CENTRAL_HOST_AUTH && window.HubAPI && typeof window.HubAPI.me === 'function') {
        return window.HubAPI.me();
      }
      return fetch('/api/me', { credentials: 'include' }).then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) {
          return { ok: r.ok, status: r.status, data: j };
        });
      });
    }
    function authResultHasUser(result) {
      var data = result && result.data ? result.data : result;
      var user = data && (data.user || data);
      return !!(result && result.ok && user && user.email);
    }
    function checkAuth() {
      if (!READY || !ENDPOINT) return;
      var probe = USE_CENTRAL_HOST_AUTH
        ? centralAuthResult()
        : fetch(ENDPOINT + '/api/me', { credentials: 'include', headers: TOKEN ? { 'Authorization': 'Bearer ' + TOKEN } : {} })
          .then(function (r) { return r.json().catch(function () { return null; }).then(function (j) { return { ok: r.ok, status: r.status, data: j }; }); });
      probe.then(function (result) {
          if (USE_CENTRAL_HOST_AUTH) {
            var signedIn = authResultHasUser(result);
            auth.checked = true;
            auth.required = !signedIn;
            auth.signedIn = signedIn;
            auth.signin = '/login?next=' + encodeURIComponent(location.pathname + location.hash);
            // The Builds shell is the only auth surface. Never render a second
            // sign-in card on top of its magic-link/central-auth gate.
            hideSigninCard();
            return;
          }
          var j = result && result.data ? result.data : result;
          auth.checked = true;
          auth.required = !!(j && j.authRequired);
          auth.signedIn = !!(j && j.signedIn);
          auth.signin = (j && j.signin) || 'https://workbench.rhobear.ai/signin';
          if (auth.required && !auth.signedIn) showSigninCard();
          else hideSigninCard();
        }).catch(function () {
          // A failed central probe must not resurrect the old Google card.
          if (USE_CENTRAL_HOST_AUTH) hideSigninCard();
        });
    }
    function showSigninCard() {
      if (USE_CENTRAL_HOST_AUTH) { hideSigninCard(); return; }
      if (signinCard && signinCard.isConnected) { thread.scrollTop = thread.scrollHeight; return; }
      signinCard = document.createElement('div');
      signinCard.className = 'rho-signin';
      signinCard.innerHTML =
        '<div class="rho-signin-title">Let\u2019s make it yours</div>' +
        '<div class="rho-signin-sub">Rho runs on your RHOBEAR credits \u2014 sign in and every chat, voice call, and scout is yours.</div>' +
        '<button class="rho-gbtn" type="button">' + GOOGLE_G + '<span>Continue with Google</span></button>';
      signinCard.querySelector('.rho-gbtn').addEventListener('click', openSignin);
      thread.appendChild(signinCard);
      thread.scrollTop = thread.scrollHeight;
    }
    function hideSigninCard() {
      if (signinCard) { signinCard.remove(); signinCard = null; }
    }
    function openSignin() {
      var url = ENDPOINT + '/api/auth/start?o=' + encodeURIComponent(location.origin);
      var w = 520, hh = 680;
      var popup = window.open(url, 'rho-signin', 'popup,width=' + w + ',height=' + hh +
        ',left=' + Math.max(0, ((screen.width || w) - w) / 2) + ',top=' + Math.max(0, ((screen.height || hh) - hh) / 2));
      if (!popup) { try { location.href = auth.signin || url; } catch (e) {} }
    }
    window.addEventListener('message', function (e) {
      if (e.origin !== EP_ORIGIN) return;
      var d = e.data;
      if (d && d.type === 'rho:token' && typeof d.token === 'string') {
        TOKEN = d.token; lsSet('rho.token', TOKEN);
        auth.signedIn = true;
        hideSigninCard();
        append('assistant', 'You\u2019re in. What are we doing first?');
      }
    });

    // ---- personalize: accent + voice --------------------------------------
    function applyAccent(hex) {
      ACCENT = hex;
      root.style.setProperty('--rho-a', hex);
      root.style.setProperty('--rho-a2', hueShift(hex, 42, 0.08, 0.04));
      root.style.setProperty('--rho-a3', hueShift(hex, -28, 0.05, 0.10));
      lsSet('rho.accent', hex);
      renderSwatches();
    }
    function renderSwatches() {
      var box = root.querySelector('#rho-swatches');
      box.innerHTML = '';
      var seen = {};
      SWATCHES.forEach(function (hex) {
        if (seen[hex.toLowerCase()]) return; seen[hex.toLowerCase()] = 1;
        var b = document.createElement('button');
        b.className = 'rho-swatch' + (hex.toLowerCase() === ACCENT.toLowerCase() ? ' rho-on' : '');
        b.style.background = 'linear-gradient(135deg, ' + hex + ', ' + hueShift(hex, 42, 0.08, 0.04) + ')';
        b.setAttribute('aria-label', 'Accent ' + hex);
        b.addEventListener('click', function () { applyAccent(hex); });
        box.appendChild(b);
      });
    }
    function renderVoices() {
      var box = root.querySelector('#rho-voices');
      box.innerHTML = '';
      VOICES.forEach(function (v) {
        var b = document.createElement('button');
        b.className = 'rho-voice' + (v === VOICE ? ' rho-on' : '');
        b.textContent = v;
        b.addEventListener('click', function () {
          VOICE = v; lsSet('rho.voice', v); renderVoices();
          speakSample(v);
        });
        box.appendChild(b);
      });
    }
    function speakSample(v) {
      if (!READY || !ENDPOINT) return;
      fetch(ENDPOINT + '/api/tts', {
        method: 'POST', headers: authHeaders(), credentials: 'include',
        body: JSON.stringify({ text: 'Hey, this is ' + TITLE + ' \u2014 sounding like ' + v + '.', voice: v, style: 'rho' })
      }).then(function (r) { return r.ok ? r.blob() : null; }).then(function (b) {
        if (!b) return;
        var a = new Audio(URL.createObjectURL(b));
        a.play().catch(function () {});
      }).catch(function () {});
    }
    renderSwatches();
    renderVoices();
    // Personalize (accent + voice) — moved off the header into the + menu so the
    // header reads mic / expand / close like the mock. Feature kept, not dropped.
    personalizeBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      plusMenu.classList.remove('rho-on');
      settings.classList.toggle('rho-on');
    });
    // header controls: mic → live voice call · expand → full-viewport surface
    callHdrBtn.addEventListener('click', function () { callOpen(); });
    expandBtn.addEventListener('click', function () { root.classList.toggle('rho-expanded'); });

    // ---- plus menu ---------------------------------------------------------
    plusBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      settings.classList.remove('rho-on');
      plusMenu.classList.toggle('rho-on');
    });
    document.addEventListener('click', function (e) {
      if (!settings.contains(e.target) && e.target !== personalizeBtn && !personalizeBtn.contains(e.target)) settings.classList.remove('rho-on');
      if (!plusMenu.contains(e.target) && e.target !== plusBtn) plusMenu.classList.remove('rho-on');
    });
    root.querySelector('#rho-pm-new').addEventListener('click', function () {
      plusMenu.classList.remove('rho-on');
      sessionId = null;
      try { localStorage.removeItem('rho.session'); } catch (e) {}
      thread.innerHTML = '';
      pendingImage = null;
      append('assistant', 'Fresh start \u2014 what are we doing?');
    });
    root.querySelector('#rho-pm-copy').addEventListener('click', function () {
      plusMenu.classList.remove('rho-on');
      var lines = [];
      thread.querySelectorAll('.rho-msg').forEach(function (m) {
        lines.push((m.classList.contains('user') ? 'You: ' : TITLE + ': ') + m.textContent);
      });
      try { navigator.clipboard.writeText(lines.join('\n')); } catch (e) {}
    });
    root.querySelector('#rho-pm-image').addEventListener('click', function () {
      plusMenu.classList.remove('rho-on');
      var fi = document.createElement('input');
      fi.type = 'file'; fi.accept = 'image/png,image/jpeg,image/webp';
      fi.addEventListener('change', function () {
        var f = fi.files && fi.files[0];
        if (!f) return;
        var rd = new FileReader();
        rd.onload = function () {
          if (typeof rd.result === 'string' && rd.result.length < 3200000) {
            pendingImage = rd.result;
            showAttachPill(rd.result, f.name);
          }
        };
        rd.readAsDataURL(f);
      });
      fi.click();
    });
    function showAttachPill(dataUrl, name) {
      var old = thread.querySelector('.rho-attach-pill');
      if (old) old.remove();
      var pill = document.createElement('div');
      pill.className = 'rho-attach-pill';
      pill.innerHTML = '<img alt="attachment" src="' + dataUrl + '"><span>' + (name || 'image') + ' rides the next message</span>';
      var x = document.createElement('button');
      x.textContent = '×'; x.setAttribute('aria-label', 'Remove attachment');
      x.addEventListener('click', function () { pendingImage = null; pill.remove(); });
      pill.appendChild(x);
      thread.appendChild(pill);
      thread.scrollTop = thread.scrollHeight;
    }

    // ---- deep thinking ------------------------------------------------------
    thinkBtn.addEventListener('click', function () {
      thinking = !thinking;
      thinkBtn.classList.toggle('rho-on', thinking);
      thinkBtn.setAttribute('aria-pressed', String(thinking));
    });

    // ---- composer -----------------------------------------------------------
    function autogrow() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 120) + 'px'; }
    function refreshSend() { sendBtn.disabled = input.value.trim().length === 0; }
    input.addEventListener('input', function () { autogrow(); refreshSend(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!sendBtn.disabled) send(); }
    });
    sendBtn.addEventListener('click', function () { if (!sendBtn.disabled) send(); });

    function append(role, text, opts) {
      opts = opts || {};
      var d = document.createElement('div');
      d.className = 'rho-msg ' + role + (opts.streaming ? ' is-streaming' : '');
      d.textContent = text;
      thread.appendChild(d);
      thread.scrollTop = thread.scrollHeight;
      return d;
    }
    function setText(node, text) { node.textContent = text; thread.scrollTop = thread.scrollHeight; }
    function setBusy(on) { root.classList.toggle('rho-busy', !!on); }

    // premium orb state machine: head + call orbs mirror Rho's lifecycle
    var _orbEls = null, _orbTimer = null;
    function orbEls() { if (!_orbEls || !_orbEls.length) _orbEls = root.querySelectorAll('.rho-head-orb .rho-orbimg, #rho-call-orb .rho-orbimg'); return _orbEls; }
    function setOrb(state, holdMs) {
      if (_orbTimer) { clearTimeout(_orbTimer); _orbTimer = null; }
      var t = orbEls();
      for (var i = 0; i < t.length; i++) t[i].setAttribute('data-state', state);
      // Drive the ONE orb engine (rho-orb-live.js) so the living canvas actually
      // animates through states — the PNG data-state is only the fallback skin.
      // rho-orb-live knows idle/thinking/speaking; map error/loading onto them.
      if (window.RhoSetState) {
        try { window.RhoSetState(state === 'error' || state === 'loading' ? 'thinking' : state); }
        catch (e) { if (window.console && console.warn) console.warn('[rho] RhoSetState failed', e); }
      }
      if (holdMs) _orbTimer = setTimeout(function () { setOrb('idle'); }, holdMs);
    }

    // ── live waveform — teal bars (pack signature). A self-driving rAF makes
    //    them travel while active; real mic RMS (WhisperSTT onLevel) sets the
    //    amplitude when listening, a steady pulse stands in while Rho speaks. ──
    var WAVE_BARS = 32, waveTargets = [], waveRAF = 0, waveAmp = 0.14, waveSeed = 0;
    function waveInit(el) {
      if (el.__bars) return;
      el.__bars = [];
      for (var i = 0; i < WAVE_BARS; i++) { var b = document.createElement('i'); el.appendChild(b); el.__bars.push(b); }
    }
    function waveLoop() {
      waveSeed += 1;
      // iterate a snapshot — a state transition can waveStop() (splice) between
      // frames; a copy keeps this frame's pass stable regardless.
      var targets = waveTargets.slice();
      for (var t2 = 0; t2 < targets.length; t2++) {
        var el = targets[t2]; if (!el.__bars) continue;
        var maxH = el.id === 'rho-call-wave' ? 34 : 20;
        for (var i = 0; i < el.__bars.length; i++) {
          var w = 0.5 + 0.5 * Math.sin(waveSeed * 0.18 + i * 0.55);
          el.__bars[i].style.height = (3 + waveAmp * maxH * w).toFixed(1) + 'px';
        }
      }
      waveRAF = waveTargets.length ? requestAnimationFrame(waveLoop) : 0;
    }
    function waveStart(el) { if (!el) return; waveInit(el); el.classList.add('rho-on'); if (waveTargets.indexOf(el) < 0) waveTargets.push(el); if (!waveRAF) waveRAF = requestAnimationFrame(waveLoop); }
    function waveStop(el) { if (!el) return; el.classList.remove('rho-on'); var k = waveTargets.indexOf(el); if (k >= 0) waveTargets.splice(k, 1); if (el.__bars) for (var i = 0; i < el.__bars.length; i++) el.__bars[i].style.height = '4px'; }
    function waveSetAmp(a) { waveAmp = Math.min(1, Math.max(0.06, a)); }

    // ---- voice follow-up: Listen turns on a deliberate, reversible mode ---
    // The visible response stays clean. The server receives the `rho` delivery
    // style separately, so speech has Rho's cadence without stage directions
    // leaking into the chat or getting read aloud as literal tags.
    var voiceFollow = {
      on: false, listening: false, queue: [], playing: false, audio: null,
      pendingSentence: '', sourceBtn: null, turnActive: false
    };
    function setVoiceFollowStatus(text, paused) {
      if (!voiceFollow.on) return;
      voiceStatus.textContent = text || (voiceFollow.listening ? 'Listening for your next turn' : 'Voice replies on');
      voicePause.innerHTML = paused ? ICON.play : ICON.pause;
      voicePause.setAttribute('aria-label', paused ? 'Play voice playback' : 'Pause voice playback');
      voicePause.title = paused ? 'Play voice playback' : 'Pause voice playback';
    }
    function clearVoiceFollowButton() {
      if (!voiceFollow.sourceBtn) return;
      voiceFollow.sourceBtn.classList.remove('rho-on');
      if (voiceFollow.sourceBtn.firstChild) voiceFollow.sourceBtn.firstChild.innerHTML = ICON.speaker;
      voiceFollow.sourceBtn = null;
    }
    function voiceFollowStopAudio() {
      voiceFollow.queue = [];
      voiceFollow.pendingSentence = '';
      if (ttsAbort) { try { ttsAbort.abort(); } catch (e) {} ttsAbort = null; }
      if (voiceFollow.audio) { try { voiceFollow.audio.pause(); } catch (e) {} voiceFollow.audio = null; }
      voiceFollow.playing = false;
      clearVoiceFollowButton();
      if (voiceFollow.on) setVoiceFollowStatus(voiceFollow.listening ? 'Listening for your next turn' : 'Voice replies on', false);
    }
    function voiceFollowPump() {
      if (!voiceFollow.on || voiceFollow.playing || !voiceFollow.queue.length) return;
      voiceFollow.playing = true;
      var sentence = voiceFollow.queue.shift();
      setVoiceFollowStatus('Rho is speaking', false);
      var controller = new AbortController();
      ttsAbort = controller;
      fetch(ENDPOINT + '/api/tts', {
        method: 'POST', headers: authHeaders(), credentials: 'include',
        body: JSON.stringify({ text: sentence, voice: VOICE, style: 'rho' }),
        signal: controller.signal
      }).then(function (r) { return r.ok ? r.blob() : null; }).then(function (blob) {
        if (!voiceFollow.on || controller.signal.aborted) return;
        ttsAbort = null;
        if (!blob) { voiceFollow.playing = false; voiceFollowPump(); return; }
        var audio = new Audio(URL.createObjectURL(blob));
        voiceFollow.audio = audio;
        audio.onplay = function () { if (voiceFollow.audio === audio) setVoiceFollowStatus('Rho is speaking', false); };
        audio.onpause = function () { if (voiceFollow.audio === audio && !audio.ended) setVoiceFollowStatus('Voice paused', true); };
        audio.onended = audio.onerror = function () {
          if (voiceFollow.audio !== audio) return;
          voiceFollow.audio = null; voiceFollow.playing = false;
          if (voiceFollow.queue.length) voiceFollowPump();
          else setVoiceFollowStatus('Voice replies on', false);
        };
        audio.play().catch(function () {
          if (voiceFollow.audio === audio) setVoiceFollowStatus('Ready to play', true);
        });
      }).catch(function () { voiceFollow.playing = false; voiceFollowPump(); });
    }
    function voiceFollowEnqueue(text) {
      text = (text || '').trim();
      if (!text || !voiceFollow.on) return;
      voiceFollow.queue.push(text);
      voiceFollowPump();
    }
    function voiceFollowAddDelta(delta) {
      if (!voiceFollow.on) return;
      voiceFollow.pendingSentence += delta || '';
      var sentence;
      while ((sentence = voiceFollow.pendingSentence.match(/^([\s\S]*?[.!?])(\s|$)/))) {
        voiceFollowEnqueue(sentence[1]);
        voiceFollow.pendingSentence = voiceFollow.pendingSentence.slice(sentence[0].length);
      }
    }
    function voiceFollowFlush() {
      if (voiceFollow.pendingSentence.trim()) voiceFollowEnqueue(voiceFollow.pendingSentence);
      voiceFollow.pendingSentence = '';
      voiceFollow.turnActive = false;
    }
    function startVoiceFollow(btn) {
      if (!READY || !ENDPOINT) return false;
      voiceFollow.on = true;
      root.classList.add('rho-follow-on');
      if (btn) {
        clearVoiceFollowButton();
        voiceFollow.sourceBtn = btn;
        btn.classList.add('rho-on');
        if (btn.firstChild) btn.firstChild.innerHTML = ICON.stopspk;
      }
      setVoiceFollowStatus('Voice replies on', false);
      return true;
    }
    function exitVoiceFollow() {
      if (!voiceFollow.on && !voiceFollow.listening) return;
      voiceFollow.on = false;
      voiceFollow.listening = false;
      if (voiceFollow.turnActive) interrupt();
      voiceFollow.turnActive = false;
      voiceFollowStopAudio();
      try { if (window.WhisperSTT) WhisperSTT.stop(); } catch (e) {}
      dictate.classList.remove('rho-on');
      waveStop(barWave);
      input.setAttribute('placeholder', input.getAttribute('data-rho-ph') || 'Message Rho…');
      root.classList.remove('rho-follow-on');
    }
    function stopMsgSpeak() { voiceFollowStopAudio(); }
    function speakMsg(text, btn) {
      if (voiceFollow.on && voiceFollow.sourceBtn === btn) { exitVoiceFollow(); return; }
      if (!startVoiceFollow(btn) || !text) return;
      voiceFollowStopAudio();
      voiceFollow.sourceBtn = btn;
      btn.classList.add('rho-on');
      if (btn.firstChild) btn.firstChild.innerHTML = ICON.stopspk;
      voiceFollowEnqueue(text);
    }
    function attachActions(node) {
      if (!node || node.__acted) return; node.__acted = 1;
      var row = document.createElement('div');
      row.className = 'rho-msg-actions';
      var copyBtn = document.createElement('button');
      copyBtn.className = 'rho-msg-act'; copyBtn.type = 'button';
      copyBtn.innerHTML = '<span style="display:inline-flex">' + ICON.copy + '</span><span>Copy</span>';
      copyBtn.addEventListener('click', function () {
        var t = node.textContent || '';
        var done = function () { copyBtn.classList.add('rho-ok'); copyBtn.lastChild.textContent = 'Copied'; setTimeout(function () { copyBtn.classList.remove('rho-ok'); copyBtn.lastChild.textContent = 'Copy'; }, 1400); };
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(done, done);
        else { try { var ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); done(); } catch (e) {} }
      });
      var listenBtn = document.createElement('button');
      listenBtn.className = 'rho-msg-act'; listenBtn.type = 'button';
      listenBtn.innerHTML = '<span style="display:inline-flex">' + ICON.speaker + '</span><span>Listen</span>';
      listenBtn.addEventListener('click', function () { speakMsg(node.textContent || '', listenBtn); });
      row.appendChild(copyBtn); row.appendChild(listenBtn);
      if (node.nextSibling) node.parentNode.insertBefore(row, node.nextSibling);
      else node.parentNode.appendChild(row);
    }

    // the send comet: a spark that arcs from the bar to the head orb
    function comet() {
      try {
        var panel = root.querySelector('#rho-panel');
        var from = root.querySelector('#rho-send').getBoundingClientRect();
        var to = root.querySelector('.rho-head-orb').getBoundingClientRect();
        var pr = panel.getBoundingClientRect();
        var c = document.createElement('span');
        c.className = 'rho-comet';
        c.style.left = (from.left - pr.left + from.width / 2 - 5) + 'px';
        c.style.top = (from.top - pr.top + from.height / 2 - 5) + 'px';
        panel.appendChild(c);
        var dx = (to.left - from.left), dy = (to.top - from.top);
        c.animate([
          { transform: 'translate(0,0) scale(1)', opacity: 1 },
          { transform: 'translate(' + dx * 0.5 + 'px,' + (dy * 0.55 - 40) + 'px) scale(.85)', opacity: 1, offset: 0.55 },
          { transform: 'translate(' + dx + 'px,' + dy + 'px) scale(.3)', opacity: 0 }
        ], { duration: 620, easing: 'cubic-bezier(.3,.6,.3,1)' }).onfinish = function () { c.remove(); };
      } catch (e) {}
    }

    // crew chips (chat thread)
    var crewBox = null, crewChips = {};
    function crewChip(key, label, task) {
      if (!crewBox || !crewBox.isConnected) {
        crewBox = document.createElement('div');
        crewBox.className = 'rho-crew';
        thread.appendChild(crewBox);
      }
      var chip = crewChips[key];
      if (!chip) {
        chip = document.createElement('span');
        chip.className = 'rho-chip-tool';
        chip.innerHTML = '<span class="rho-dot"></span><span class="rho-chip-name"></span><span class="rho-chip-task"></span>';
        crewBox.appendChild(chip);
        crewChips[key] = chip;
      }
      chip.querySelector('.rho-chip-name').textContent = label;
      if (task) chip.querySelector('.rho-chip-task').textContent = '\u2014 ' + task;
      thread.scrollTop = thread.scrollHeight;
      return chip;
    }
    function crewDone(key) {
      var chip = key == null ? null : crewChips[key];
      if (chip) chip.classList.add('rho-done');
    }
    function crewAllDone() {
      Object.keys(crewChips).forEach(function (k) { crewChips[k].classList.add('rho-done'); });
      crewChips = {};
      crewBox = null;
    }

    async function send() {
      var text = input.value.trim();
      if (!text) return;
      // Capture this turn's mode once. A Stop tap during the request clears the
      // audio queue and aborts the request; it must not make a later SSE delta
      // unexpectedly start speaking again.
      var voiceReply = voiceFollow.on;
      stopMsgSpeak();
      if (READY && ENDPOINT && auth.checked && auth.required && !auth.signedIn) {
        // keep their words in the box \u2014 sign in, then hit send again
        showSigninCard();
        return;
      }
      input.value = ''; autogrow(); refreshSend();
      var bubble = append('user', text);
      bubble.classList.add('rho-sendoff');
      comet();
      var attach = thread.querySelector('.rho-attach-pill');
      if (attach) attach.remove();

      // FROZEN-READY: bot backend not live yet -> graceful local reply, no dead network call.
      if (!READY || !ENDPOINT) {
        var warm = append('assistant', '', { streaming: true });
        typeOut(warm, WARMING);
        return;
      }

      var node = append('assistant', '', { streaming: true });
      sendBtn.disabled = true;
      setBusy(true);
      setOrb('thinking');
      if (voiceReply) {
        voiceFollow.turnActive = true;
        voiceFollow.pendingSentence = '';
        setVoiceFollowStatus('Rho is thinking', false);
      }
      try {
        await askBrain(text, {
          image: pendingImage || undefined,
          thinking: thinking,
          mode: voiceReply ? 'voice' : 'text',
          onDelta: function (d, full) {
            if (!node.__spoke) { node.__spoke = 1; setOrb('speaking'); }
            setText(node, full);
            if (voiceReply && voiceFollow.on) voiceFollowAddDelta(d);
          },
          onTool: function (name) { crewChip('t:' + name, name); },
          onToolDone: function () {},
          onAgent: function (id, kind, task) { crewChip('a:' + id, kind, task); },
          onTask: function (id, status, summary) {
            var chip = crewChip('a:' + id, 'task', summary || status);
            if (status === 'completed' || status === 'done') chip.classList.add('rho-done');
          }
        });
        node.classList.remove('is-streaming');
        crewAllDone();
        setOrb('idle');
        if (!node.textContent) {
          var fallbackReply = "…I didn't catch a reply that time. Try me again?";
          setText(node, fallbackReply);
          if (voiceReply && voiceFollow.on) voiceFollowEnqueue(fallbackReply);
        }
        if (voiceReply && voiceFollow.on) voiceFollowFlush();
        attachActions(node);
      } catch (err) {
        node.classList.remove('is-streaming');
        crewAllDone();
        if (voiceReply) voiceFollow.turnActive = false;
        if (err && err.rho === 'signin') {
          node.remove();
          setOrb('idle');
          if (!USE_CENTRAL_HOST_AUTH) showSigninCard();
        } else if (err && err.rho === 'credits') {
          setOrb('idle');
          setText(node, err.message + ' Top up at workbench.rhobear.ai.');
        } else {
          setOrb('error', 1100);
          setText(node, "I hit a snag reaching the crew. Give it another go in a sec.");
        }
      } finally {
        pendingImage = null;
        setBusy(false);
        refreshSend();
      }
    }

    // typewriter for the warming message (no network)
    function typeOut(node, text) {
      var i = 0;
      setBusy(true);
      setOrb('thinking');
      (function step() {
        node.textContent = text.slice(0, i);
        thread.scrollTop = thread.scrollHeight;
        if (i++ < text.length) setTimeout(step, 14);
        else { node.classList.remove('is-streaming'); setBusy(false); setOrb('idle'); attachActions(node); }
      })();
    }

    // ---- the brain call: full SSE surface ----------------------------------
    async function askBrain(text, h) {
      var chatId = 'c' + Math.random().toString(36).slice(2);
      activeChatId = chatId;
      var controller = new AbortController();
      activeAbort = controller;
      var payload = {
        text: (h.thinking ? '(Take your time and think this through carefully before answering.) ' : '') + text,
        sessionId: sessionId, chatId: chatId, mode: h.mode || 'text'
      };
      if (h.image) payload.image = h.image;
      var resp = await fetch(ENDPOINT + '/api/chat', {
        method: 'POST',
        headers: authHeaders(),
        credentials: 'include',
        body: JSON.stringify(payload),
        signal: controller.signal
      });
      if (resp.status === 401) {
        auth.signedIn = false;
        var e401 = new Error('signin required'); e401.rho = 'signin'; throw e401;
      }
      if (resp.status === 402) {
        var j402 = null; try { j402 = await resp.json(); } catch (e) {}
        var e402 = new Error((j402 && j402.message) || 'Out of credits.'); e402.rho = 'credits'; throw e402;
      }
      if (!resp.ok || !resp.body) throw new Error('brain ' + resp.status);
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = '', full = '', brainError = null;
      for (;;) {
        var r = await reader.read();
        if (r.done) break;
        buf += decoder.decode(r.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) !== -1) {
          var raw = buf.slice(0, idx); buf = buf.slice(idx + 2);
          var ev = 'message', data = null;
          raw.split('\n').forEach(function (line) {
            if (line.indexOf('event:') === 0) ev = line.slice(6).trim();
            else if (line.indexOf('data:') === 0) { try { data = JSON.parse(line.slice(5)); } catch (e) {} }
          });
          if (ev === 'session' && data && data.sessionId) {
            sessionId = data.sessionId;
            try { localStorage.setItem('rho.session', sessionId); } catch (e) {}
          } else if (ev === 'delta' && data) {
            full += (data.text || '');
            if (h.onDelta) h.onDelta(data.text || '', full);
          } else if (ev === 'tool' && data) {
            if (h.onTool) h.onTool(data.name || 'tool', data.agent);
          } else if (ev === 'tool_done' && data) {
            if (h.onToolDone) h.onToolDone(data.id);
          } else if (ev === 'agent' && data) {
            if (h.onAgent) h.onAgent(data.id, data.kind || 'scout', data.task || '');
          } else if (ev === 'task' && data) {
            if (h.onTask) h.onTask(data.id, data.status, data.summary);
          } else if (ev === 'error' && data) {
            brainError = new Error(data.message || 'brain error');
          }
        }
      }
      activeAbort = null; activeChatId = null;
      if (brainError && !full) throw brainError;
      return full;
    }

    function interrupt() {
      if (activeAbort) { try { activeAbort.abort(); } catch (e) {} }
      if (activeChatId && ENDPOINT) {
        fetch(ENDPOINT + '/api/interrupt', {
          method: 'POST', headers: authHeaders(), credentials: 'include',
          body: JSON.stringify({ chatId: activeChatId })
        }).catch(function () {});
      }
      activeAbort = null; activeChatId = null;
    }

    // ---- dictation: WHISPER, local in-browser -------------------------------
    // Doctrine [[whisper-is-the-stt-standard]]: never webkitSpeechRecognition
    // (worse + streams mic audio to Google + flaky). whisper-stt.js runs Whisper
    // in the visitor's own browser \u2014 private, no server. Lazy-loaded from our
    // origin the first time the mic is tapped; a progress pill shows the one-time
    // model download, then it's cached and instant.
    // NOTE: bump WHISPER_VER on every whisper-stt.js change — Cloudflare caches
    // no-query static JS for hours, so a fresh ?v= is how the update actually
    // reaches users (query-string = cache miss).
    var WHISPER_VER = '12';
    var WHISPER_JS = (function () {
      var q = '/whisper-stt.js?v=' + WHISPER_VER;
      try { var o = new URL(document.currentScript && document.currentScript.src || '').origin; if (o && o !== 'null') return o + q; } catch (e) {}
      return 'https://builds.rhobear.ai' + q;
    })();
    function ensureWhisper(cb) {
      if (window.WhisperSTT) return cb();
      var s = document.getElementById('rho-whisper-js');
      if (s) { s.addEventListener('load', cb); return; }
      s = document.createElement('script'); s.id = 'rho-whisper-js'; s.src = WHISPER_JS;
      s.onload = function () { cb(); };
      s.onerror = function () { dictate.style.display = 'none'; };
      document.head.appendChild(s);
    }
    // Plain dictation is intentionally boring: tap to record, tap again to
    // finish, review the words, then send when ready. Voice follow-up changes
    // only after a person explicitly presses Listen beneath an answer.
    var plainDictating = false;
    function composerPlaceholder() { return input.getAttribute('data-rho-ph') || 'Message Rho…'; }
    function resetComposerDictation() {
      plainDictating = false;
      dictate.classList.remove('rho-on');
      waveStop(barWave);
      input.setAttribute('placeholder', composerPlaceholder());
    }
    function startPlainDictation() {
      ensureWhisper(function () {
        if (!window.WhisperSTT || !WhisperSTT.available() || !WhisperSTT.dictate) { dictate.style.display = 'none'; return; }
        var original = input.getAttribute('placeholder') || 'Message Rho…';
        input.setAttribute('data-rho-ph', original);
        if (plainDictating) { WhisperSTT.dictate({}); return; }
        plainDictating = true;
        dictate.classList.add('rho-on');
        input.setAttribute('placeholder', 'Listening — tap the mic when you are done');
        waveStart(barWave);
        WhisperSTT.dictate({
          onStatus: function (st) {
            st = String(st || '');
            if (st === 'transcribing') input.setAttribute('placeholder', 'Transcribing…');
            else if (st.indexOf('error:') === 0 || st.indexOf('mic:') === 0) {
              resetComposerDictation();
              input.setAttribute('placeholder', st.indexOf('mic:') === 0 ? 'Microphone permission is off' : 'Transcription hit a snag');
              setTimeout(function () { if (!voiceFollow.on) input.setAttribute('placeholder', original); }, 3000);
            }
          },
          onText: function (text) {
            resetComposerDictation();
            if (text && text.trim()) {
              input.value = (input.value ? input.value.trim() + ' ' : '') + text.trim();
              autogrow(); refreshSend();
              try { input.focus(); } catch (e) {}
            }
          }
        });
      });
    }
    function startVoiceFollowTurn() {
      ensureWhisper(function () {
        if (!voiceFollow.on) return;
        if (!window.WhisperSTT || !WhisperSTT.available() || !WhisperSTT.talk) { exitVoiceFollow(); return; }
        var original = input.getAttribute('placeholder') || 'Message Rho…';
        input.setAttribute('data-rho-ph', original);
        voiceFollow.listening = true;
        dictate.classList.add('rho-on');
        waveStart(barWave);
        setVoiceFollowStatus('Listening — tap mic to return to dictation', false);
        input.setAttribute('placeholder', 'Listening — pause naturally when you are done');
        WhisperSTT.talk({
          onLevel: function (rms) { if (voiceFollow.listening) waveSetAmp(rms * 3.4); },
          onStatus: function (st) {
            st = String(st || '');
            if (!voiceFollow.on) return;
            if (st === 'transcribing') { setVoiceFollowStatus('Transcribing your turn', false); input.setAttribute('placeholder', 'Transcribing…'); }
            else if (st.indexOf('error:') === 0 || st.indexOf('mic:') === 0) exitVoiceFollow();
          },
          onFinal: function (text) {
            if (!voiceFollow.on) return;
            voiceFollow.listening = false;
            dictate.classList.remove('rho-on');
            waveStop(barWave);
            input.setAttribute('placeholder', original);
            if (!text || !text.trim()) { setVoiceFollowStatus('Voice replies on', false); return; }
            // Put the transcript in the normal composer first so the user can
            // see exactly what was heard, then send this voice-mode turn.
            input.value = text.trim(); autogrow(); refreshSend();
            send();
          }
        });
      });
    }
    dictate.addEventListener('click', function () {
      if (voiceFollow.on) {
        // The second mic tap is the escape hatch the user asked for: quit the
        // smart voice path completely and leave ordinary dictation untouched.
        if (voiceFollow.listening) exitVoiceFollow();
        else startVoiceFollowTurn();
        return;
      }
      startPlainDictation();
    });
    voicePause.addEventListener('click', function () {
      var audio = voiceFollow.audio;
      if (!audio) return;
      if (audio.paused) audio.play().catch(function () {});
      else audio.pause();
    });
    voiceStop.addEventListener('click', exitVoiceFollow);

    /* ---- THE BIG ONE: live voice call --------------------------------------
       Continuous listening -> brain -> spoken reply, sentence by sentence.
       States: listening / thinking / speaking. Tap the orb to interrupt. */
    var call = {
      on: false, state: 'idle', rec: null,
      queue: [], playing: false, audio: null,
      pendingSentence: '', spokenFull: ''
    };

    function callSetState(s) {
      call.state = s;
      root.classList.remove('rho-v-listening', 'rho-v-thinking', 'rho-v-speaking');
      if (s !== 'idle') root.classList.add('rho-v-' + s);
      setOrb(s === 'thinking' ? 'thinking' : s === 'speaking' ? 'speaking' : 'idle');
      callState.textContent = s === 'listening' ? 'listening…' : s === 'thinking' ? 'thinking…' : s === 'speaking' ? 'speaking…' : '';
      // waveform rides the whole call: mic RMS while listening, a steady pulse
      // while Rho speaks (the mock shows the strip alive during "speaking…").
      if (s === 'idle') { waveStop(callWave); }
      else { waveStart(callWave); if (s !== 'listening') waveSetAmp(s === 'speaking' ? 0.5 : 0.18); }
    }

    function callCrewChip(label, task) {
      var chip = document.createElement('span');
      chip.className = 'rho-chip-tool';
      chip.innerHTML = '<span class="rho-dot"></span><span></span>';
      chip.children[1].textContent = label + (task ? ' \u2014 ' + task : '');
      callCrew.appendChild(chip);
      while (callCrew.children.length > 4) callCrew.removeChild(callCrew.firstChild);
      return chip;
    }

    function ttsEnqueue(sentence) {
      var s = sentence.trim();
      if (!s) return;
      call.queue.push(s);
      ttsPump();
    }
    function ttsPump() {
      if (call.playing || !call.queue.length || !call.on) return;
      call.playing = true;
      var s = call.queue.shift();
      fetch(ENDPOINT + '/api/tts', {
        method: 'POST', headers: authHeaders(), credentials: 'include',
        body: JSON.stringify({ text: s, voice: VOICE, style: 'rho' })
      }).then(function (r) { return r.ok ? r.blob() : null; }).then(function (b) {
        if (!call.on) { call.playing = false; return; }
        if (!b) { call.playing = false; ttsPump(); return; }
        callSetState('speaking');
        var a = new Audio(URL.createObjectURL(b));
        call.audio = a;
        a.onended = a.onerror = function () {
          call.playing = false; call.audio = null;
          if (call.queue.length) ttsPump();
          else if (call.on && !activeAbort) { callSetState('listening'); callListen(); }
        };
        a.play().catch(function () { a.onended(); });
      }).catch(function () { call.playing = false; ttsPump(); });
    }
    function ttsStop() {
      call.queue = [];
      if (call.audio) { try { call.audio.pause(); } catch (e) {} call.audio = null; }
      call.playing = false;
    }

    function callListen() {
      // WHISPER live ear (doctrine [[whisper-is-the-stt-standard]]): record with
      // silence-VAD, transcribe locally, take the turn. Re-arms itself after Rho
      // speaks (ttsPump) for continuous back-and-forth.
      if (!call.on) return;
      ensureWhisper(function () {
        if (!call.on || !window.WhisperSTT) return;
        WhisperSTT.talk({
          onLevel: function (rms) { if (call.state === 'listening') waveSetAmp(rms * 3.4); },
          onStatus: function (st) {
            st = '' + st;
            if (st === 'listening' && call.state !== 'speaking') callSetState('listening');
            // mic denied / transcribe error → don't stick on "connecting"; tell
            // the user and drop to idle so a tap on the orb can retry.
            else if (call.on && (st.indexOf('mic') === 0 || st.indexOf('error') === 0)) {
              callLine.textContent = st.indexOf('mic') === 0
                ? 'I can’t hear a mic — check the browser’s mic permission, then tap the orb to retry.'
                : 'Voice hit a snag — tap the orb to try again.';
              callSetState('idle');
            }
          },
          onFinal: function (t) {
            if (!call.on) return;
            if (t && t.trim()) { callLine.innerHTML = '<span class="rho-heard"></span>'; callLine.firstChild.textContent = t; callTurn(t.trim()); }
            else if (call.state === 'listening') callListen();   // heard nothing → re-open the ear
          }
        });
      });
    }

    async function callTurn(text) {
      try { if (window.WhisperSTT) WhisperSTT.stop(); } catch (e) {}
      callSetState('thinking');
      callCrew.innerHTML = '';
      call.spokenFull = ''; call.pendingSentence = '';
      var replyShown = '';
      try {
        await askBrain(text, {
          mode: 'voice',
          onDelta: function (d, full) {
            replyShown = full;
            callLine.textContent = full;
            // sentence pipeline: speak as soon as a sentence completes
            call.pendingSentence += d;
            var m;
            while ((m = call.pendingSentence.match(/^([\s\S]*?[.!?])(\s|$)/))) {
              ttsEnqueue(m[1]);
              call.pendingSentence = call.pendingSentence.slice(m[0].length);
            }
          },
          onTool: function (name) { callCrewChip(name); },
          onAgent: function (_id, kind, task) { callCrewChip(kind, task); },
          onTask: function () {}
        });
        if (call.pendingSentence.trim()) { ttsEnqueue(call.pendingSentence); call.pendingSentence = ''; }
        if (!replyShown) callLine.textContent = '…';
        // if nothing is queued/speaking (empty reply), go straight back to listening
        if (!call.queue.length && !call.playing && call.on) { callSetState('listening'); callListen(); }
      } catch (err) {
        if (err && err.rho === 'signin') {
          // voice can't sign you in \u2014 drop to the chat panel, card's waiting
          callClose(); open();
          return;
        }
        if (err && err.rho === 'credits') {
          if (call.on) { callLine.textContent = err.message + ' Top up at workbench.rhobear.ai.'; callSetState('listening'); callListen(); }
          return;
        }
        if (call.on) {
          callLine.textContent = 'I hit a snag \u2014 say that again?';
          callSetState('listening'); callListen();
        }
      }
    }

    function callOpen() {
      if (!READY || !ENDPOINT) { open(); return; }
      if (auth.checked && auth.required && !auth.signedIn) { open(); return; }
      exitVoiceFollow();
      call.on = true;
      root.classList.add('rho-call-open');
      callCrew.innerHTML = '';
      callLine.textContent = '';
      // audio unlock on the user gesture so replies are allowed to play
      try {
        var unlock = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=');
        unlock.play().catch(function () {});
      } catch (e) {}
      if (navigator.mediaDevices && window.MediaRecorder) { callSetState('listening'); callListen(); }
      else { callSetState('idle'); callLine.textContent = 'This browser has no mic input \u2014 type to me in the chat instead.'; }
    }
    function callClose() {
      call.on = false;
      ttsStop();
      interrupt();
      try { if (window.WhisperSTT) WhisperSTT.stop(); } catch (e) {}
      call.rec = null;
      callSetState('idle');
      waveStop(callWave);
      root.classList.remove('rho-call-open');
    }
    callBtn.addEventListener('click', callOpen);
    callExit.addEventListener('click', callClose);
    callOrb.addEventListener('click', function () {
      // tap = cut in: stop the voice, stop the brain, hand the floor back
      ttsStop();
      interrupt();
      if (call.on) { callSetState('listening'); callListen(); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && root.classList.contains('rho-call-open')) callClose();
    });

    // Host-page bridge: surfaces (Workbench composer, Plans chrome, the Hub)
    // can open the chat or jump straight into the live call.
    window.RhoEmbed = {
      version: '2.5',
      open: open,
      close: close,
      call: callOpen,
      endCall: callClose,
      setAccent: applyAccent
    };
  }

  if (document.body) mount();
  else document.addEventListener('DOMContentLoaded', mount);
})();
