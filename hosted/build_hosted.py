#!/usr/bin/env python3
"""Generate hosted/app/index.html from the base single-file app (../index.html).

The base app is the proven, shipped, localStorage-only version. This script
applies a small set of SURGICAL, anchored transforms to turn it into the hosted
multi-user / synced / offline client, without hand-forking 1,900 lines — so the
free app stays the single source of truth for all CRM logic and any future fix
to it flows into the hosted build by re-running this script.
"""
import os, sys, re

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(HERE, '..', 'index.html')
OUT  = os.path.join(HERE, 'app', 'index.html')
GLUE = os.path.join(HERE, 'app', '_glue.inc.js')

def die(msg):
    print('BUILD FAILED: ' + msg); sys.exit(1)

def replace_once(s, old, new, label):
    if s.count(old) < 1:
        die('anchor not found: ' + label)
    return s.replace(old, new, 1)

src = open(BASE, encoding='utf-8').read()
glue = open(GLUE, encoding='utf-8').read()

# --- 1. PWA meta + manifest in <head> (after <title>) ----------------------
src = replace_once(src,
    '<title>PowerWash CRM</title>',
    '<title>PowerWash CRM</title>\n'
    '<meta name="theme-color" content="#0a84ff" />\n'
    '<link rel="manifest" href="manifest.webmanifest" />\n'
    '<meta name="apple-mobile-web-app-capable" content="yes" />\n'
    '<meta name="mobile-web-app-capable" content="yes" />\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="default" />\n'
    '<meta name="apple-mobile-web-app-title" content="PowerWash" />\n'
    '<link rel="apple-touch-icon" href="icons/icon-192.png" />\n'
    '<link rel="icon" href="icons/icon-192.png" />',
    'head PWA meta')

# --- 2. Auth-gate + sync CSS (before </style>) -----------------------------
css = r'''
/* ---- hosted: auth gate, sync pill, account ---- */
.hb-auth{min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:24px;background:var(--bg)}
.hb-card{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:20px;padding:32px 28px;box-shadow:0 12px 40px rgba(0,0,0,.10);text-align:center}
.hb-logo{width:56px;height:56px;border-radius:16px;background:linear-gradient(180deg,#3aa0ff,#0a6fe0);display:flex;align-items:center;justify-content:center;margin:0 auto 16px;color:#fff}
.hb-logo svg{width:30px;height:30px}
.hb-card h1{font-size:22px;margin:0 0 6px;letter-spacing:-.02em}
.hb-sub{color:var(--muted);font-size:14px;line-height:1.5;margin:0 0 18px}
.hb-card code{background:var(--chip);padding:1px 6px;border-radius:6px;font-size:12.5px}
.hb-l{display:block;text-align:left;font-size:12.5px;font-weight:600;color:var(--muted);margin:10px 0 5px}
.hb-card input{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:12px;font-size:15px;background:var(--input,var(--card));color:var(--ink);box-sizing:border-box}
.hb-btn{width:100%;margin-top:16px;padding:13px;border:none;border-radius:12px;background:var(--accent);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
.hb-btn:disabled{opacity:.6;cursor:default}
.hb-link{background:none;border:none;color:var(--accent);font-size:13.5px;cursor:pointer;margin-top:14px}
.hb-foot{margin-top:18px;color:var(--faint);font-size:12.5px;line-height:1.5}
.hb-msg{background:var(--chip);border-radius:12px;padding:12px 14px;font-size:13.5px;color:var(--ink);margin-bottom:6px;line-height:1.5}
.hb-pill{font-size:11.5px;font-weight:600;padding:3px 9px;border-radius:999px;border:none;cursor:pointer;letter-spacing:.01em}
.hb-pill.ok{background:rgba(52,199,89,.14);color:#1f9d4d}
.hb-pill.busy{background:rgba(10,132,255,.14);color:#0a6fe0}
.hb-pill.err{background:rgba(255,149,0,.16);color:#c26a00}
.hb-pill.off{background:rgba(142,142,147,.18);color:#6b6b70}
.hb-member{font-size:13.5px;margin:2px 0}
.hb-tag{font-size:10.5px;background:var(--accent);color:#fff;border-radius:999px;padding:1px 7px;margin-left:4px;vertical-align:middle}
[data-theme="dark"] .hb-card input{background:#1c1c1e}
</style>'''
src = replace_once(src, '</style>', css, 'auth CSS')

# --- 3. Inject external scripts before the main app <script> ---------------
src = replace_once(src, '\n<script>\n',
    '\n<script src="vendor/supabase.js"></script>\n'
    '<script src="config.js"></script>\n'
    '<script src="sync.js"></script>\n'
    '<script src="db.js"></script>\n'
    '<script>\n',
    'external scripts')

# --- 4. UUID id generators (Supabase PKs are uuid) -------------------------
src = replace_once(src,
    "function uid(){ return 'l'+Date.now().toString(36)+Math.random().toString(36).slice(2,7); }",
    "function uid(){ return hbUUID(); }", 'uid()')
src = replace_once(src,
    "function cid(){ return 'c'+Date.now().toString(36)+Math.random().toString(36).slice(2,5); }",
    "function cid(){ return hbUUID(); }", 'cid()')

# --- 5. Persistence -> Supabase-backed ------------------------------------
old_load = ("function load(){\n"
            "  try{ var raw=localStorage.getItem(STORE_KEY); if(raw){ state.leads=JSON.parse(raw).leads||[]; return true; } }catch(e){}\n"
            "  return false;\n"
            "}")
src = replace_once(src, old_load,
    "function load(){ return false; /* hosted: data comes from Supabase, see boot() */ }",
    'load()')
old_save = ("function save(){\n"
            "  try{ localStorage.setItem(STORE_KEY, JSON.stringify({v:1,leads:state.leads})); }\n"
            "  catch(e){ toast('Could not save — storage may be full',true); }\n"
            "}")
src = replace_once(src, old_save,
    "function save(){ try{ if(HB.ready) HB.persist(); }catch(e){} }",
    'save()')

# --- 6. Role guard: only owners delete (RLS also enforces this) ------------
src = replace_once(src,
    "function deleteLead(id){\n"
    "  var idx=state.leads.findIndex(function(l){return l.id===id;}); if(idx<0) return;",
    "function deleteLead(id){\n"
    "  if(HB.role==='agent'){ toast('Only the owner can delete leads',true); return; }\n"
    "  var idx=state.leads.findIndex(function(l){return l.id===id;}); if(idx<0) return;",
    'deleteLead guard')
src = replace_once(src, "  else if(act==='erase') eraseAll();",
    "  else if(act==='erase'){ if(HB.role==='agent'){ toast('Only the owner can erase data',true); } else eraseAll(); }",
    'erase guard')

# --- 7. Topbar: sync pill + account button in the sidebar ------------------
src = replace_once(src,
    "'<button data-act=\"settings\">'+I.gear+'Settings</button>'+",
    "'<button data-act=\"account\"><span id=\"hb-acct-label\">Account</span></button>'+\n"
    "      '<button data-act=\"settings\">'+I.gear+'Settings</button>'+",
    'sidebar account btn')
src = replace_once(src,
    "'<button class=\"btn btn-primary\" data-act=\"add\">'+I.plus+'<span>Add Lead</span></button>'+",
    "'<button class=\"hb-pill ok\" id=\"hb-sync\" data-act=\"sync\" title=\"Sync status\">Synced</button>'+\n"
    "    '<button class=\"btn btn-primary\" data-act=\"add\">'+I.plus+'<span>Add Lead</span></button>'+",
    'topbar sync pill')

# --- 8. Wire new data-act handlers ----------------------------------------
src = replace_once(src, "  else if(act==='export') exportData();",
    "  else if(act==='export') exportData();\n"
    "  else if(act==='account') openModal(hbAccountModal());\n"
    "  else if(act==='sync'){ HB.flushNow(); hbPull(); toast(HB.online?'Syncing…':'You\\'re offline — changes are saved on this device'); }\n"
    "  else if(act==='hb-invite') hbInvite();\n"
    "  else if(act==='hb-import') hbImportFree();\n"
    "  else if(act==='hb-signout') hbSignOut();",
    'act handlers')

# --- 9. Replace boot() + its call with the hosted glue + hosted boot -------
boot_marker = "/* ============================================================================ BOOT */"
if boot_marker not in src:
    die('boot marker not found')
# remove the original boot() function and its invocation, keep seed() gone-unused
orig_boot = src[src.index(boot_marker):]
# everything from the marker to the closing of boot(); (the "boot();" line)
m = re.search(r"/\* =+ BOOT \*/.*?\nboot\(\);", src, re.S)
if not m:
    die('could not match original boot block')
hosted_block = ("/* ============================================================================ HOSTED BOOT */\n"
                + glue + "\nboot();")
src = src[:m.start()] + hosted_block + src[m.end():]

# --- sanity: no leftover localStorage lead persistence --------------------
if "localStorage.setItem(STORE_KEY" in src:
    die('STORE_KEY persistence still present')

os.makedirs(os.path.dirname(OUT), exist_ok=True)
open(OUT, 'w', encoding='utf-8').write(src)
print('OK wrote ' + OUT + '  (%d bytes)' % len(src))
