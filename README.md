# PowerWash CRM

A clean, simple lead-tracking dashboard for a power washing business — built to solve the "oh, I should really reach back out to them" problem. It sits **alongside** Workiz and Go High Level: this is where you track leads, remember follow-ups, and log every conversation, all in one calm, Apple-style interface.

## How to open it

**It's a single file — no installing anything.**

- **Easiest:** double-click `index.html` and it opens in your browser (Chrome, Safari, Edge — any of them).
- **Bookmark it** so it's one click away. On a Mac you can drag the tab to your Dock.
- Works on your phone too — it adapts to the smaller screen.

Your data is saved automatically in that browser on that device. No login, no cloud, no monthly fee.

> **Back up your data:** click **Export / back up** in the bottom-left now and then — it downloads a small file with everything in it. The app nudges you if it's been more than two weeks. **Import leads** loads it back (or pulls in a CSV — see below). The app starts with a few sample leads so you can see how it works; click **Clear samples** when you're ready.

## Bringing in your existing leads

Click **Import leads** (bottom-left or in Settings) and pick a file:

- **A CSV export** from Workiz, Go High Level, a Facebook leads download, or a spreadsheet. The app reads your column headings and **matches them automatically** (name, phone, email, address, value, source, notes) — you just glance to confirm, choose whether the batch is mostly residential or commercial, and import. It **skips duplicates** by phone/email so re-importing is safe.
- **A JSON backup** (a file you exported from this app) to restore everything.

## What it does

**Everything filters by Residential vs Commercial** using the toggle at the top.

### 📊 Dashboard
Your morning check-in: active leads, pipeline value, wins, and — front and center — a **"Needs your attention"** list of everyone overdue or due this week, plus a list of active leads with **no next step scheduled** so nobody slips through.

### 📋 Pipeline
A drag-and-drop board: **New → Contacted → Quoted → Scheduled → Won / Lost.** Cards show the job value, service tags, and a red/amber flag when a follow-up is late or coming up. Drag a lead to **Lost** and it quietly asks why (price, competitor, no response…) so you can spot patterns later.

### 🔔 Follow-ups
Everyone who needs a touch, grouped **Overdue / Due today / This week / Upcoming**. The red badge in the sidebar is your overdue count — get it to zero and you're caught up. **Snooze** any item 3 days with one tap.

### 👥 Contacts
A sortable, searchable table of every lead, with a service-type filter.

### 📈 Insights
Your numbers at a glance: **win rate** (counted only on *closed* leads — won + lost — so unworked leads never skew it), **win rate broken down by lead quality** (see how well your Hot vs Cold leads actually close), **revenue won**, **average job value**, open pipeline, **which lead sources actually convert** (so you know where to spend), most-requested services, and a pipeline funnel.

### The lead record (click any lead)
- **Contact info** — phone, email, address are tappable (call, email, open in Maps).
- **Pipeline stage** — change it from a dropdown.
- **Lead quality** — mark each lead **Hot / Warm / Cold** (one tap). A colored dot shows it everywhere, and Insights breaks down your win rate by quality.
- **Follow-up reminder** — set a "reach back out on ___" date with a note; quick buttons *or pick any date you want*.
- **One-tap outreach** — hit **Text** or **Email** and your phone's Messages/email app opens **pre-filled** with a message; you just tap send. Optionally logs it for you.
- **Communication log** — a timeline of every call, text, email, or on-site visit, with date and notes. Logging a contact **prompts you to set the next follow-up right then**.
- **Service tags** — house wash, driveway/concrete, roof, deck/fence, gutters, fleet, storefront, and more — **plus any custom tags you add yourself**.
- **Repeat business** — mark a Won job to re-clean in 6, 12, or **any custom number of months** and it auto-schedules the reminder. Power washing wears off; this books the next job before you forget.
- **Notes** — gate codes, surfaces, pricing, anything.

## Messages, tone & templates

You set a **tone** (Friendly / Professional / Casual) and the built-in follow-up and win-back messages match it. You can also **write your own templates** in Settings using fill-in-the-blanks like `{first}` (first name), `{place}` (home/property), `{service}`, and `{value}` — they fill in automatically per lead. Everything is one-tap **Text** / **Email** / **Copy**.

> This is *semi-automatic*: the message is written for you and your phone opens ready to go — you tap send (about 10 seconds). Truly automatic sending (on a schedule, no tapping) would need a small hosted backend + a texting/email service; that's an easy future add-on when you want it.

## Reminders

Three ways, use whichever fits:
- **Desktop notifications** — turn on in Settings; a pop-up when follow-ups are due (with permission).
- **Reminders on your phone** — click **Calendar** in Settings (or "Add to my calendar" on any lead) to download a calendar file. Open it once and your phone's calendar notifies you — and can email you too.
- **In-app** — the red sidebar badge and the Dashboard "Needs attention" list are always there.

## Make it yours
- **Custom service tags** — tap "+ New" in the Services picker on any lead, or manage them in Settings.
- **Custom lead sources** — add your own (e.g. Nextdoor) in Settings.
- **Message templates & tone** — in Settings.
- **Theme** — light or dark.

## Handy shortcuts
- `N` — new lead
- `/` — jump to search
- `Esc` — close any panel

## Where your data lives (important)
Everything is stored privately in the browser you're using — nothing leaves your device. That's what makes it free and instant, but it also means:
- **The web link and the downloaded file keep separate data.** Pick one to be your "real" one.
- **Export a backup regularly** (the app reminds you). To move to a new computer or phone, export on the old one and import on the new one.

## A note on automation
Because this is a private, no-server tool, it doesn't auto-send texts or emails — instead it makes reaching out frictionless (one-tap call/email/map links, copy-ready templates, and reminders that surface exactly when it's time). If you later want true auto-send (scheduled email/SMS) or a shared team version with a login, that's a natural next step and would need a small backend — easy to build on this same foundation.
