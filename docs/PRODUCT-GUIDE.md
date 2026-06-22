# SpatiaMed — Product & Feature Guide

**Last updated:** 2026-06-12 · **Maintained in:** `spatiamed-common/docs/`

This is the single reference for **what SpatiaMed provides today**, how each feature is used, and what is still in flight. Two audiences:

- **Hospitals / prospects** → read §1 (At a Glance) and the per-product "What you get" sections.
- **Internal team** → everything, especially §9 (Status Board: built vs. in progress vs. planned).

---

## 1. The Platform at a Glance

SpatiaMed is a multi-tenant SaaS suite for Indian hospitals, sold as one plan (**₹12,000/month + ₹25,000 one-time setup, 14-day free pilot, no credit card**). Two patient-facing products run on a shared platform:

| Product | What it does |
|---|---|
| **QueueCare** | OPD queue management — self-service kiosk check-in, live token queues, TV display boards, WhatsApp/SMS/voice notifications, doctor & reception dashboards, HMS integration, and **telemedicine** (video consults with live multilingual captions and AI-drafted clinical notes). |
| **CareLoop** | Patient engagement & growth — WhatsApp/SMS campaigns, automated nurture sequences, an AI voice agent (10+ Indian languages), Google review collection with AI-drafted replies, doctor referrals, and channel-level ROI attribution. |

```
                        ┌─────────────────┐
   spatiamed.com  ───▶  │  Platform API    │  identity · billing · onboarding
   (signup wizard)      └──────┬───────────┘
                               │ JWT (tenant_id, role, products)
            ┌──────────────────┼──────────────────┐
            ▼                  ▼                   ▼
     ┌────────────┐    ┌────────────┐      ┌──────────────┐
     │  QueueCare │◀──▶│  CareLoop  │      │ Hospital-    │  staff frontend
     │  (+ kiosk) │    │            │      │ portal       │  for both products
     └─────┬──────┘    └────────────┘      └──────────────┘
           │ webhooks / long-poll                 telemedicine-web (video consults)
           ▼                                      display boards · token tracker
     hms-connector-agent (on-prem, inside hospital network)
           ▼
     Hospital HMS (Bahmni · MocDoc · generic REST)
```

**Repos:** `platform-api`, `QueueCare` (server + notification_service + kiosk), `CareLoop`, `Hospital-portal`, `telemedicine-web`, `SpatiaMed-web` (marketing), `spatiamed-common` (shared library), `hms-connector-agent` (on-prem agent).

---

## 2. Platform API — Identity, Billing, Onboarding

The control plane. Issues the JWTs every other service validates; owns tenants, staff, subscriptions, and usage limits.

### What you get
- **Self-serve signup** (3-step wizard on spatiamed.com): hospital details → trial-or-paid choice → atomic provisioning (tenant + subscription + owner account + onboarding checklist), with QueueCare and CareLoop notified automatically.
- **14-day free trial** with reminder emails (4 days / 1 day left) and automatic suspension on expiry; paid path via **Razorpay** (checkout, recurring billing, webhook-driven lifecycle: active → past_due → suspended → churned).
- **Staff management**: email invites, roles (`owner` / `admin` / `staff`), password reset, deactivation. Per-staff product access flags.
- **Usage metering & plan limits**: departments, doctors, monthly AI calls / WhatsApp / SMS — counted in Redis, reported by products, enforced before expensive operations.
- **Brand assets**: logo/favicon uploads to AWS S3; portal theming flows from these.
- **Per-tenant journey config**: workflow "knobs" (approval levels, routing prefs) the products consume.
- **Super-admin console** (internal): tenant list/suspend/reactivate/extend-trial, WhatsApp/SMS **number-pool** management, MRR metrics.

### How it's used
- Hospitals: sign up at `spatiamed.com/onboard`; owner receives portal URL + temp password; complete onboarding checklist; go live.
- Services: validate the Platform JWT locally (shared HS256 secret; claims: `tenant_id`, `platform_role`, `products`). Service-to-service calls use `X-Internal-Secret`.

**Security:** PostgreSQL Row-Level Security on all tenant tables, three-role DB posture (app/migrations/system users), PII encrypted at rest (AES-256-GCM) with salted-hash lookup columns.

---

## 3. QueueCare — OPD Queue Management

### 3.1 Token & queue engine
- **Token lifecycle state machine**: waiting → near_turn → called → in_consultation → completed, with missed/no_show/skip/requeue paths (2nd miss = permanent). Redis sorted-set queues with **priority weighting** — Emergency > Appointment > Senior (60+) > Normal — and compensation rollback so a failed DB write never loses a patient's place.
- **Token numbers** per department per day (`CARD-001` style), printed/shown at the kiosk.
- **Miss timer**: a called patient who doesn't show within the configured window (default 120s) is automatically marked missed via Redis keyspace expiry.

### 3.2 Patient self-check-in (kiosk tablet app)
Expo/React Native tablet app, provisioned with a one-time setup code from the portal.
- **6 languages** (English, Hindi, Tamil, Telugu, Kannada, Malayalam).
- Flow: language → department (live wait stats) → doctor (availability badges) → patient info (10-digit phone auto-finds returning patients; age ≥ 60 auto-priority) → token screen with queue position & ETA, auto-resets after 15s.
- **Offline-first**: department/doctor lists cached in SQLite; check-ins queue locally and sync when connectivity returns; deterministic offline token allocation.
- **Pre-registration aware**: patients pre-registered via CareLoop are recognized by phone and fast-tracked to a confirm screen.

### 3.3 Live visibility
- **TV display board** (`/display/:deptId`, no login): "now serving" + upcoming tokens, WebSocket/SSE-driven with polling fallback.
- **Patient token tracker** (`/t/:tokenId`, no login): patients follow their own token status, position, and ETA on their phone in real time.
- **Reception & doctor dashboards** in the Hospital-portal (real-time via WebSocket): register patients, call next, complete/skip/no-show, transfer.

### 3.4 Notifications (multi-channel, consent-aware)
RabbitMQ-driven sidecar service sends **WhatsApp (Gupshup) → SMS (MSG91) fallback → voice calls (Exotel + Sarvam TTS in the patient's language)** on token-created / near-turn / called / missed events. Pre-flight checks: token state still current, event freshness, distributed lock, dedup, **DPDP consent per channel**, idempotency. SMS fallback fires only on real delivery failures (not when WhatsApp simply isn't configured for the tenant).

### 3.5 Appointments, payments & identity rails
Appointment booking + pre-registration promotion, **Razorpay** payments, **ABDM** (ABHA health-ID) and **DigiLocker** integrations, doctor-recommendation engine (condition → specialty mapping), per-tenant queue/kiosk settings.

### 3.6 Telemedicine (separate SKU on the shared backend)
- **Consultation encounters**: scheduled video consults tied to bookings; provider-agnostic (Daily.co, Google Meet, CareLoop RTC, stub) behind one `VideoProvider` interface.
- **Patient experience** (`telemedicine-web`): join via tokenized link → **consent gate** (transcription consent, 11 languages) → full-screen video with a **live caption rail** (speaker-labelled captions + source text, translation across EN/HI/BN/TA/RU/JA/ES/FR/AR/PT/ZH).
- **Doctor experience**: consultation room inside the Hospital-portal with the same live captions.
- **AI clinical scribe**: after the call, the transcript is captured (encrypted, S3-stored), and a Temporal saga runs LLM extraction → **draft clinical note + draft prescription** for the doctor to review and sign. Drafts only — the system never signs or sends anything autonomously (platform-wide human-in-the-loop posture).
- **e-Prescription compliance engine**: allopathy/ayurveda regime rules evaluate every prescription (block/warn violations) before sign-off; PDF rendering for signed notes; WhatsApp/email delivery of clinical reports via CareLoop.
- **Per-tenant Google Meet OAuth** (free/degraded tier): hospitals connect their own Google account from the portal; refresh tokens stored encrypted under RLS (`tenant_credentials` table); Meet links are created on the hospital's calendar. Daily.co remains the embedded, captions-capable primary.

### 3.7 HMS Integration (works with the hospital's existing HMS)
- **Canonical model + vendor adapters** (in `spatiamed-common`): Bahmni/OpenMRS, MocDoc, and a field-mapped **generic REST** adapter for Tier-2 vendors; CSV import for batch onboarding.
- **Sync engines**: inbound appointment events → QueueCare bookings; slot engine + decision engine reconcile HMS slots with QueueCare's queue; Temporal sagas orchestrate write-backs (check-in, consultation start, finalize) with idempotency and conflict handling.
- **On-prem connector agent** (`hms-connector-agent`): a Docker container deployed inside the hospital network. Outbound-only (no firewall changes): polls the HMS locally and forwards canonical events to QueueCare; long-polls QueueCare for write-back commands and ACKs results. Configured entirely by `.env`; ships with `docker-compose.yml`.
- **Portal UX**: HMS dashboard with **Activity Strip** (live sync feed) and **Review Later Inbox** — ambiguous bookings are queued for human review, never auto-resolved.

---

## 4. CareLoop — Patient Engagement & Growth

### 4.1 Campaigns & sequences
- **Multi-channel campaigns** (WhatsApp, SMS, voice, email) with draft → active → paused lifecycle, per-channel filters, and metrics.
- **Sequence engine**: state-machine-driven nurture journeys (e.g. post-visit follow-ups, recall reminders) with per-tenant journey configuration. All outbound passes through one idempotent sender + communication log — and through the compliance stack below.

### 4.2 Compliance stack (built for Indian healthcare regulation)
- **NMC content guardrails**: blocks superlatives, outcome guarantees, testimonials in any outbound marketing.
- **TRAI DLT**: SMS templates must be DLT-registered; unregistered templates are blocked; auto-registration on approval.
- **TCCCPR**: voice calls only 9 AM–9 PM IST. **NDNC** scrubbing service (registry API wiring pending).
- **DPDP**: per-channel patient consent records (insert-only), per-tenant data-retention tiers, PII encrypted at rest everywhere.

### 4.3 Smart Templates (AI-generated, compliance-gated)
Admins describe what they need; **AI drafts the template** (WhatsApp/SMS/voice), a **two-pass compliance scan** (regex + LLM) flags NMC/DLT issues with suggestions, then an **adaptive approval chain** (0/1/2 levels, configurable per tenant) routes to marketing manager / compliance officer before anything can be sent.

### 4.4 AI voice agent (10+ languages)
Inbound and outbound calls handled by a LangGraph agent over a real-time pipeline (Exotel/Twilio/Telnyx telephony → Sarvam or Deepgram STT → LLM → Sarvam or ElevenLabs TTS):
- Identifies the caller, detects intent, fills slots (e.g. appointment booking), confirms, executes, closes — with **compliance guard** and **progressive autonomy** gates, consent collection, human-handoff and escalation paths, and graceful degradation chains when a vendor fails.
- **Languages**: Indian languages via Sarvam (compliance-proven), international tier via Deepgram/ElevenLabs. Per-tenant language and per-country telephony-provider routing.
- **Voice sagas** (Temporal): scripted reminder/confirmation calls (synthesize → call → transcribe → detect confirm/decline).
- Call recordings policy per tenant (off / with-consent / required) with retention enforcement.

### 4.5 Reviews, referrals & attribution
- **Review collection**: post-visit review solicitation; sentiment + topic analysis on incoming reviews; **AI-drafted replies** (grateful/empathetic tone by rating) for staff to approve; reviews dashboard with star distributions and trends.
- **Doctor referrals**: per-source QR codes / links (`/refer/{slug}`), WhatsApp referral classification (AI), referral leaderboards.
- **Attribution**: channel-level spend vs. revenue, ROAS / CAC / LTV per channel, revenue tied back to actual completed visits via QueueCare webhooks (phone-hash identity joins, never raw phone numbers).

---

## 5. Staff & Patient Surfaces

| Surface | Audience | Highlights |
|---|---|---|
| **Hospital-portal** (web) | All staff | Role-gated modules: reception/doctor queue dashboards, HMS inbox, consultation room w/ captions, QueueCare analytics, full CareLoop suite (campaigns, calls + transcripts, reviews, referrals, attribution), 8 settings pages (hospital, departments, doctors, services, staff, QueueCare, CareLoop, billing), setup wizard. Tenant-branded theming with WCAG contrast checks. |
| **Kiosk** (tablet) | Patients at reception | §3.2 — 6-language self-check-in, offline-capable. |
| **Display board** (TV) | Waiting room | §3.3 — public, real-time. |
| **Token tracker** (phone) | Patients | §3.3 — public link per token. |
| **telemedicine-web** | Patients (telemedicine) | §3.6 — video consult + consent + live multilingual captions. |
| **SpatiaMed-web** | Prospects | Marketing site, pricing, product pages, blog (12 articles), demo scheduling, 3-step signup. |

**Role matrix (portal):** owner → billing + everything; admin → settings + analytics; doctor → doctor queue + consultations; receptionist → reception queue; marketing manager → CareLoop; compliance officer → review/template approvals.

---

## 6. Shared Foundation (`spatiamed-common`, v0.2.0)

One pip package consumed by every Python service:

| Module | Capability |
|---|---|
| `encryption` | AES-256-GCM field encryption (key-versioned, Fernet-legacy migration) + transport encryption for webhook PII |
| `phone` | Indian phone normalization + salted SHA-256 hashing — the **one canonical patient identity** across services |
| `webhook_auth` | HMAC-SHA256 signing/verification with replay protection |
| `gupshup` / `msg91` / `truecaller` | WhatsApp, SMS, verified-caller-ID clients |
| `dlt` / `nmc_guardrails` / `compliance` | DLT template validation, NMC content scanning, TCCCPR call-window rules |
| `integrations` | HMS canonical types, `HmsAdapter` interface, Bahmni / MocDoc / generic-REST / CSV adapters, typed error hierarchy |

**AI stack (platform-wide, June 2026):** **Gemini is the primary LLM everywhere** (`gemini-3.1-flash-lite` for the voice agent's low-latency loop, `gemini-3.5-flash` for clinical extraction); **Claude (`claude-sonnet-4-6` / `claude-haiku-4-5`) is the fallback** when Gemini is unreachable, and powers CareLoop's Haiku-tier tasks (sentiment, drafts, templates, compliance pass-2, referral classification). Voice: Sarvam (`saarika:v2.5` STT, `bulbul:v3` TTS) for Indian languages; Deepgram `nova-3` + ElevenLabs `flash_v2_5` for international. Video: Daily.co (embedded, captions) with Google Meet as the per-tenant free tier. Every AI feature is **recommend-only**: drafts and suggestions are surfaced to staff; a human approves, signs, or sends.

---

## 7. What a Hospital Gets (plain language)

1. **Day-1 queue management** — kiosk check-in in 6 languages, TV boards, WhatsApp updates to patients, dashboards for staff. Deployed in ~2 days.
2. **Fewer no-shows, fuller OPD** — automated reminders, missed-call recovery, recall campaigns, an AI receptionist that answers and books in the patient's language.
3. **Reputation growth** — automatic review collection with AI-drafted, staff-approved replies; referral tracking with QR codes; a dashboard that shows which channel actually brings revenue.
4. **Telemedicine** — video consults with live captions across languages, AI-drafted notes and compliant e-prescriptions that the doctor reviews and signs.
5. **Works with your HMS** — Bahmni, MocDoc, or any REST-capable system, via a small on-prem connector that needs **no firewall changes**; ambiguous syncs land in a human review inbox.
6. **Compliance built-in** — NMC advertising rules, TRAI DLT, TCCCPR call windows, DPDP consent & encryption. AI never acts alone.

---

## 8. Architecture & Security Posture (internal)

- **Tenant isolation, three layers**: JWT claim → `SET LOCAL` session variable → PostgreSQL RLS policy on every tenant table (FORCE ROW LEVEL SECURITY; boot-time self-test asserts coverage). Runtime DB roles cannot bypass RLS.
- **PII**: encrypted at rest (AES-256-GCM, key-versioned), phone identity via salted hashes only; insert-only consent and approval audit tables; PHI access log.
- **Cross-service**: HMAC-signed webhooks with outbox/retry/dead-letter (QueueCare ↔ CareLoop); internal endpoints behind shared secrets + host allow-lists.
- **Orchestration**: Temporal (sagas for voice calls, transcript→extraction, HMS write-backs); Celery for CareLoop workers; RabbitMQ for notification fan-out.
- **Key management**: platform-pooled vendor keys (env/deploy secrets) + per-tenant credentials (encrypted rows in `tenant_credentials`, first provider: Google Meet OAuth). See spec `QueueCare/server/docs/specs/2026-06-12-tenant-credentials-google-meet-oauth.md`.

---

## 9. Status Board — Built · In Progress · Planned

> Verified against code on 2026-06-12 (beads ledgers in some repos lag behind merged work; code state wins).

### ✅ Built & shipped
- **Platform API**: full PRD-001 — auth/JWT, onboarding, billing (Razorpay), staff, limits, admin console, number pool.
- **QueueCare core**: full PRD-005 — token engine, kiosk, display boards, notifications sidecar, ABDM/DigiLocker/Razorpay, RLS Phase B.
- **CareLoop**: full PRD-002 — campaigns, sequences, AI voice agent (incl. international voice tier), Smart Templates with compliance scan + adaptive approvals, reviews, referrals, attribution.
- **CareLoop acquisition gaps G1–G4** (PRD-addendum-acquisition): missed-call → 60s AI-callback lead capture, health-camp QR/form registration (`/camp/{slug}`, tenant-gated), `profile.lead_created` event + `pre_visit_welcome` nurture sequence, ad-click session stitching (`attribution_sessions` + `tracking_id`, `/attribution/ack`). Migrations 0030–0033. Only the portal lead-source filter (addendum §2.8, "minimal UI") remains.
- **Live-video call path end-to-end**: call-start orchestration (room.started → consent gate → caption pipeline), patient consent + language pick, transcript handoff → extraction, fallback tiers, dedicated transcription bot worker (Redis-stream dispatched), **real daily-python side-car binding**, and one-tap **switch-to-audio** (video → Exotel PSTN, patient resolved via phone-hash at provision time).
- **Hospital-portal**: all 10 build phases — dashboards, settings, role gating, tenant theming; HMS Activity Strip + Review Later Inbox (Plan G).
- **HMS integration Plans A–G**: sm-common adapters, QueueCare sync/slot/decision engines, Temporal sagas, portal UX — implemented; **connector agent (Plan E)** built, tested, and published (`spatiamed/hms-connector-agent`).
- **Telemedicine**: encounters + provider abstraction, telemedicine-web consult UI with consent + live captions/translation, transcript capture, LLM extraction → draft note + draft Rx, e-prescription compliance engine, PDF render + report delivery, clinical sign-off gates, per-tenant Google Meet OAuth (tenant_credentials).
- **Marketing site**: shipped, maintenance mode.

### 🔄 In progress / partially wired
- **Smart Template Stage 3**: multi-level approval *rollout* to hospitals (code complete; enablement per tenant pending).
- **Platform-auth Phase A cutover**: dual-JWT validation shipped; `PLATFORM_AUTH_ENABLED` flip is a deployment step.
- **Hospital-portal**: setup-wizard file-upload step; Smart-Template editor screens (`portal-qez.3–7`); lead-source filter (acquisition addendum §2.8).

### 🗓 Planned (not started)
- **Per-tenant usage metering for billing** of video/AI minutes (spec'd as follow-up in tenant-credentials spec).
- **BYO enterprise vendor keys** (same `tenant_credentials` table, reserved provider values).
- **Self-hosted LiveKit provider** for video at scale (>~500–1,000 consults/month; flat-cost infra replaces per-minute Daily pricing).
- **Scalability tech-debt**: RabbitMQ HA, audit-table partitioning, PG read replicas, Redis HA, PgBouncer rollout (deferred until ~50 hospitals).

### 🚦 Go-live gates (deployment, not code)
- Flip live flags per environment: `use_live_providers`, `video_consult_use_live`, `translation_use_live` (CareLoop); `TELEMEDICINE_VIDEO_USE_LIVE`, `EXTRACTION_USE_LIVE`, `HMS_INTEGRATION_ENABLED`, `INTEGRATION_ENABLED`, `PLATFORM_AUTH_ENABLED` (QueueCare).
- Vendor credentials & account specifics: Daily domain (replaces `your-domain.daily.co`), Google OAuth client (`GOOGLE_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI`), Sarvam/Deepgram/ElevenLabs/Gemini/Anthropic keys.
- Confirm Google Meet Conference-Records transcript schema before enabling Meet transcript fetch.
- NDNC registry API wiring (currently stubbed).
- Add a **live-smoke vendor test suite** — stub-gated adapters rot invisibly (this bit us with Sarvam/Deepgram/ElevenLabs request shapes; all fixed 2026-06-12).
- Cross-border PHI legal sign-off for international video/voice; production three-role DB credentials; Sentry wiring.

---

## Appendix: Repo Map

| Repo | What lives there | Stack |
|---|---|---|
| `platform-api` | Identity, billing, onboarding, admin | FastAPI · PG(RLS) · Redis · Razorpay · S3 |
| `QueueCare/server` | Queue engine, telemedicine, HMS sync, clinical notes/Rx | FastAPI · PG(RLS) · Redis · RabbitMQ · Temporal |
| `QueueCare/notification_service` | Notification consumer (WhatsApp/SMS/voice) | FastAPI · RabbitMQ · Gupshup/MSG91/Exotel+Sarvam |
| `QueueCare/kiosk` | Patient check-in tablet app | Expo SDK 55 · RN 0.83 · SQLite offline |
| `CareLoop` | Engagement engine, AI voice agent, compliance | FastAPI · Celery · LangGraph · pipecat · Temporal |
| `Hospital-portal` | Staff web frontend (both products) | React 19 · Vite · TanStack Query · WebSocket/SSE |
| `telemedicine-web` | Telemedicine patient UI | React 19 · Daily.co SDK |
| `SpatiaMed-web` | Marketing site + signup wizard | Next.js 15 · Razorpay · Resend |
| `spatiamed-common` | Shared library (this repo) | Python pkg, v0.2.0 |
| `hms-connector-agent` | On-prem HMS bridge | Python 3.12 · asyncio · Docker |
