# PowerWash CRM — Setup Guide (start to finish)

Follow these steps in order. Do exactly what each one says. Don't skip any.
When a step says **click** something, the thing to click is in **bold**.

You will need:
- A computer (not a phone) for the setup. About **20 minutes**.
- An email address you can check.

It's all free. There is no coding. You will copy and paste a few times.

At the end you'll have a web link that is your CRM. You and your VA sign in to
it, and it works on your phone and laptop.

---

# PART 1 — Get the files onto your computer

1. Go to this web page: **https://github.com/maxgelfman-glitch/Sales-Dashboard**
2. Near the top right of the page, find the green button that says **`< > Code`**.
   Click it.
3. A little menu drops down. At the bottom of it, click **Download ZIP**.
4. A file downloads to your computer (usually into your **Downloads** folder).
   It's named something like `Sales-Dashboard-main.zip`.
5. Find that file in Downloads and **double‑click it** to unzip it.
   - On a **Mac** it unzips by itself into a folder.
   - On **Windows**, a window opens — click **Extract all**, then **Extract**.
6. You now have a **folder**. Open it. Inside, open the folder named **`hosted`**.
   Keep this window open — you'll come back to it. Inside `hosted` you should see
   a folder called **`app`** and a folder called **`supabase`**.

✅ Checkpoint: you can see the `hosted` folder with `app` and `supabase` inside.

---

# PART 2 — Make your free database (Supabase)

This is where your leads are stored and backed up.

1. Go to **https://supabase.com**
2. Click **Start your project** (top right).
3. Sign up. The easiest way is **Continue with GitHub** (you just made a GitHub
   visit — if you have an account use it; if not, sign up with your **email**
   instead). Follow the prompts until you're signed in.
4. If it asks you to create an **organization**: type any name (like your
   business name), leave the other options as they are, and click **Create
   organization**.
5. Now click the green **New project** button.
6. Fill in the form:
   - **Name:** type `powerwash-crm`
   - **Database Password:** click **Generate a password**, then click the
     **copy** icon and paste it somewhere safe (a note on your computer). You
     probably won't need it, but keep it.
   - **Region:** pick the one closest to where you live.
   - Leave everything else as is.
7. Click **Create new project**.
8. Wait about **2 minutes**. You'll see a spinner / "Setting up project." When
   it's done you land on the project dashboard.

✅ Checkpoint: you're looking at your new project's dashboard.

---

# PART 3 — Set up the database (one copy‑paste)

1. On the left side of the Supabase page there's a vertical menu of icons. Find
   the one that looks like a terminal / **`SQL`** and is labeled **SQL Editor**.
   Click it.
2. Click **New query** (or you'll already see an empty text box called an editor).
3. Now go back to the **`hosted` folder** on your computer (from Part 1). Open
   the **`supabase`** folder. Inside, find the file named **`setup.sql`**.
4. Open `setup.sql` so you can copy its text:
   - **Mac:** right‑click `setup.sql` → **Open With** → **TextEdit**.
   - **Windows:** right‑click `setup.sql` → **Open with** → **Notepad**.
5. Select **all** of the text in that file: click inside it, then press
   **Ctrl+A** (Windows) or **Cmd+A** (Mac). It all turns blue/highlighted.
6. Copy it: **Ctrl+C** (Windows) or **Cmd+C** (Mac).
7. Go back to the Supabase **SQL Editor** box. Click inside it and paste:
   **Ctrl+V** (Windows) or **Cmd+V** (Mac). The box fills with text.
8. Click the green **Run** button (bottom right of the editor). Wait a couple
   seconds.

✅ Checkpoint: a message appears that says **Success** (or "Success. No rows
returned"). That's correct — it means your database is built. If you see a red
error, you didn't paste the whole file; redo steps 5–8.

---

# PART 4 — Copy your two connection values

Your app needs two things from Supabase to connect: a **Project URL** and a
**key**. Let's grab them.

1. On the left menu, scroll to the bottom and click **Project Settings** (a gear
   ⚙️ icon).
2. In the settings menu that appears, click **API** (if you see **API Keys**,
   that works too).
3. You'll see a box labeled **Project URL** with something like
   `https://abcdefgh.supabase.co`. Click the **copy** icon next to it.
4. Open a blank note on your computer and paste it there so you don't lose it.
   Label it "URL".
5. Back on the same Supabase page, find the section **Project API keys**. Find
   the key labeled **`anon`** **`public`**. Click the **copy** icon next to it.
   (It's a very long string of letters and numbers.)
6. Paste that into your note too. Label it "KEY".

✅ Checkpoint: your note has two lines — the URL and the long key.

> The `anon public` key is meant to live in a web page — it's safe. Your data is
> protected by the security rules you installed in Part 3, not by hiding this key.

---

# PART 5 — Put the app on the internet (free, drag‑and‑drop)

We'll use **Netlify**. You just drag your `app` folder onto their page and they
give you a web link.

1. Go to **https://app.netlify.com/drop**
2. You'll see a big dashed box that says to drag a folder here. (If it asks you
   to sign up first, do — **Sign up** with email or GitHub, it's free, then come
   back to **https://app.netlify.com/drop**.)
3. Go back to your **`hosted`** folder on your computer. You should see the
   **`app`** folder.
4. **Drag the entire `app` folder** onto the dashed box on the Netlify page and
   let go. (Drag the folder itself — not the files inside it.)
5. Wait a few seconds while it uploads. Netlify then shows a link near the top
   like **`https://random-words-1234.netlify.app`**. Click it to open — or copy
   it.
6. **Bookmark this link.** This is your CRM. This is the link you and your VA
   will always use.

✅ Checkpoint: clicking your Netlify link opens the app and it shows a
**"Connect your app"** screen with two boxes.

---

# PART 6 — Connect the app (paste your two values)

1. On that **"Connect your app"** screen, in the box labeled **Project URL**,
   paste the **URL** from your note (Part 4).
2. In the box labeled **anon public key**, paste the **KEY** from your note.
3. Click **Connect**.

✅ Checkpoint: the screen changes to a **sign‑in** screen asking for your email.
(If it says the URL or key looks wrong, re‑check that you pasted the right one in
the right box — the URL starts with `https://` and ends with `.supabase.co`; the
key is the long one.)

---

# PART 7 — Sign in (this creates your account)

1. On the sign‑in screen, type **your email address**.
2. Click **Email me a magic link**.
3. Open your email inbox and look for an email from Supabase (check the **spam**
   folder if you don't see it in a minute). Open it and click the **link/button**
   inside it. **Important:** open that link on the **same computer and browser**
   you're setting up on.
4. You land back in the app, signed in. This first sign‑in automatically creates
   **your business** and makes **you the owner** (you can do everything,
   including delete leads).

✅ Checkpoint: you see the CRM dashboard (empty for now — no leads yet).

---

# PART 8 — Bring in your existing leads (optional)

If you used the free browser version before and saved a backup file from it:

1. In the CRM, click **Account** (in the left menu).
2. Click **Import**.
3. Choose the backup file you exported from the free version.
4. Your leads load in. Done.

If you don't have a backup file, skip this — just start adding leads with the
**Add Lead** button.

---

# PART 9 — Add your VA

1. Click **Account** in the left menu.
2. Under **Invite a teammate**, type your VA's email address.
3. Click **Invite**.
4. Text or email your VA the **same Netlify link** (from Part 5) and tell them:
   - Open the link.
   - On the "Connect your app" screen, paste the **same URL and KEY** (send them
     your note from Part 4 — these two values are the same for everyone).
   - Sign in with **their own email**.
5. They're now in **your** business as an **agent**: they can add and edit leads,
   but only you (the owner) can delete. Everything they do is labeled with their
   name.

---

# PART 10 — Put it on your phone

Open your **Netlify link** on your phone's web browser, then:

- **iPhone (use Safari):** tap the **Share** button (the square with an arrow
  pointing up) → scroll down → tap **Add to Home Screen** → **Add**.
- **Android (use Chrome):** tap the **⋮** menu (top right) → tap **Install app**
  or **Add to Home screen**.

The first time you open it on the phone, do the "Connect your app" paste (Part 6)
and sign in (Part 7) once. After that it opens like a normal app, and it even
works with no signal — it syncs when you're back online.

---

# 🎉 You're done

Your leads now live in the cloud, back themselves up, and stay in sync across
your phone, your laptop, and your VA's devices.

---

## If something goes wrong

- **App shows "Connect your app" again after I already did it** → you're on a
  new device or browser. Just paste the URL and KEY again and sign in. That's
  normal — do it once per device.
- **The magic‑link email never arrives** → check your **spam** folder; make sure
  you typed your email correctly; wait a minute and try **Email me a magic link**
  again.
- **The database step (Part 3) showed a red error** → you probably didn't copy
  the whole `setup.sql` file. Go back, select all (Cmd/Ctrl+A), copy, paste into
  Supabase, and Run again. It's safe to run more than once.
- **My VA can't see my leads** → make sure you invited their exact email in Part
  9 **before** they signed in, and that they signed in with that same email.

## Good to know

- **Backups:** your data lives in Supabase's cloud database, which they back up.
  You can also click **Account → Export** any time to download your own copy.
- **Cost:** Supabase and Netlify are free for your size of business. (If you ever
  grew huge, Supabase's paid plan is about $25/month — you're nowhere near that.)
- **Two people editing the same lead at the same time:** whoever saves last wins
  for that field; nothing gets lost or scrambled. One rare case: if someone edits
  a lead offline while it gets deleted on another device, the edit brings the
  lead back (we keep the edit rather than lose someone's work).
- **Per‑device preferences:** your custom message templates and custom lead
  sources currently live on each device separately (they don't sync yet). Your
  leads, contacts, stages, follow‑ups, and team **do** sync everywhere.
- **The free version still works** and is completely separate — keep using either
  or both.

---

### For a developer (skip if that's not you)

If you'd rather bake the two connection values into the files so no one ever sees
the "Connect your app" screen: open `app/config.js`, put your Project URL and
anon key in the two marked spots, save, and upload the `app` folder. The setup
SQL is also available as individual migrations in `supabase/migrations/` if you
prefer running them one by one. Architecture and the test suite are documented in
`README.md`.
