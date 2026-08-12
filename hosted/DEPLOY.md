# Putting PowerWash CRM online (hosted version)

This is the **hosted** version of your lead tracker. Unlike the free single‑file
version, this one:

- **Syncs across all your devices** — add a lead on your phone at a job, it's on
  your laptop at home.
- **Backs itself up automatically** in the cloud (your data lives in a real
  database, not just one browser).
- **Has separate logins** for you and your VA, with roles: **you (owner)** can do
  everything including delete; **your VA (agent)** can add and edit leads but
  can't delete them.
- **Works offline.** On a job with no signal, it still opens and lets you add and
  edit. It syncs the moment you're back online.
- **Installs like an app** on your phone's home screen.

You'll set it up once. It takes about **15–20 minutes** and needs two free
accounts: **Supabase** (the database + logins) and a **static host** (where the
web page lives — Netlify or Vercel, both free). No coding.

---

## Step 1 — Create your database (Supabase)

1. Go to **https://supabase.com** and sign up (free). Click **New project**.
2. Give it a name (e.g. `powerwash-crm`), set a database password (save it
   somewhere), pick the region closest to you, and click **Create**.
3. Wait ~2 minutes for it to finish setting up.

## Step 2 — Create the tables and security rules

1. In your Supabase project, click **SQL Editor** in the left sidebar → **New
   query**.
2. Open each file in **`supabase/migrations/`** in order and run each one (New
   query → paste → **Run** → "Success"):
   - `0001_init.sql` — tables + security rules
   - `0002_rpc_invites.sql` — account setup + teammate invites
   - `0003_lock_role.sql` — locks roles so only you (owner) can change them

   (If more `00NN_*.sql` files are ever added, run them in number order too.)

That's your whole database — tables for leads, contacts, your team, plus the
security rules that keep each business's data private and enforce the
owner/agent permissions.

## Step 3 — Get your two connection values

1. In Supabase, click **Project Settings** (gear icon) → **API**.
2. Copy two things:
   - **Project URL** (looks like `https://abcdefgh.supabase.co`)
   - **anon public** key (a long string). *This key is safe to put in the web
     page — the security rules from Step 2 are what actually protect your data.*

## Step 4 — Put those values into the app

Open **`app/config.js`** in a text editor. Replace the two placeholders:

```js
url:     'https://YOUR-PROJECT.supabase.co',   // your Project URL
anonKey: 'YOUR-ANON-KEY',                       // your anon public key
```

Save the file.

> Tip: you can skip editing the file and instead add
> `?supabase_url=...&supabase_key=...` to the web address once — the app
> remembers it in that browser. Editing `config.js` is cleaner for the real
> thing.

## Step 5 — Put the app on the web (Netlify, easiest)

The app **must** be served over **https** (that's what makes offline + install
work). The simplest free option:

1. Go to **https://app.netlify.com/drop**.
2. Drag the **entire `app/` folder** onto the page.
3. Netlify gives you a link like `https://your-name.netlify.app`. That's your
   CRM. Bookmark it.

*(Vercel works too: `npm i -g vercel`, then run `vercel` inside the `app/`
folder. Or any static host — GitHub Pages, Cloudflare Pages, etc. The only
requirement is https.)*

## Step 6 — Sign in and you're live

1. Open your new link. Enter your email → **Email me a magic link**.
2. Check your email, click the link (open it **on the same device/browser**).
   You're in. This first sign‑in automatically creates **your business** and
   makes **you the owner**.
3. If you used the free version before, open **Account → Import** and upload the
   backup file you exported from it — all those leads come straight in.

## Step 7 — Add your VA

1. Click **Account → Invite a teammate**, enter your VA's email, **Invite**.
2. Tell your VA to open the same link and sign in with **that** email. They'll
   automatically join **your** business as an **agent** (add/edit, no delete).
   Everything they do is attributed to them.

## Step 8 — Install it on your phone

- **iPhone (Safari):** open the link → Share → **Add to Home Screen**.
- **Android (Chrome):** open the link → menu → **Install app** / **Add to Home
  Screen**.

Now it opens full‑screen like a normal app and works offline.

---

## Good to know

- **Backups.** Your data is in Supabase's cloud database, which is backed up by
  them. You can also still use **Account → Export** any time to download a copy.
- **Free tier limits.** Supabase's free tier is generous — far more than a
  single power‑washing business's lead volume. If you ever outgrow it, their
  paid tier is ~$25/mo.
- **Magic‑link emails.** Supabase sends sign‑in emails for you out of the box.
  For heavy use or your own "from" address, you can plug in an email provider in
  Supabase → Authentication → Email settings (optional).
- **Two people editing the same lead at once.** Whoever saves last wins for that
  field — the app never loses or corrupts the record. In normal owner + VA use
  this effectively never comes up.
- **Per‑device preferences.** Your custom message templates, custom lead sources
  and property types currently live per‑device (they don't sync yet). Your
  leads, contacts, stages, follow‑ups and team **do** sync. Syncing preferences
  too is a small future addition.
- **The free version still works.** This hosted version is separate and doesn't
  touch it — keep using either or both.

## If something looks off

- **"Almost there — connect your Supabase project"** on load → `config.js` still
  has the placeholders, or the URL/key has a typo. Recheck Step 4.
- **Magic link doesn't arrive** → check spam; make sure the email matches; try
  the "Use a password instead" option only if you set a password in Supabase.
- **Works on laptop but not installing on phone** → make sure you opened the
  **https** Netlify/Vercel link, not a local file.
