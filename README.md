# Krillearn ⚡

**Turn your lecture notes into a live multiplayer quiz battle.**

Upload a PDF or PowerPoint → Google Gemini AI generates questions → Share a room code → Race your friends to answer.

> Built with React + Vite · Flask + Socket.IO · Google Gemini AI · Real-time WebSockets

---

## ✨ Features

| Feature | Description |
|---|---|
| 🤖 **AI Question Generation** | Google Gemini reads your PDF/PPTX and creates unique short-answer questions |
| 🎯 **Fuzzy Answer Matching** | Typos and partial answers accepted — `"Gready bst firsst searc"` matches `"Greedy best first search"` |
| 🔤 **Acronym Support** | Abbreviations auto-accepted — type `GBFS` for `Greedy best first search` |
| ⏱ **Adaptive Timers** | AI assigns each question a time tier: ⚡ Flash (10s) · Standard (15s) · 🧠 Reasoning (25s) · 🧮 Calculation (45s) |
| 🔄 **Multi-Attempt Input** | Keep typing until the clock runs out — wrong answers show a retry warning, not a lock |
| 🏆 **Live Leaderboard** | Gold 🥇 Silver 🥈 Bronze 🥉 medals with rank-change badges after every question |
| 📡 **100% Server-Authoritative** | All timers, scoring, and phase transitions run on the backend — no client-side cheating |
| ♻️ **Fresh Questions on Replay** | Play Again serves unplayed questions from the pool — no repeats |
| 🎮 **Automatic Round Flow** | No host clicking required: questions → 5s leaderboard → next question → round break → repeat |
| 🔗 **Room Codes** | 6-character codes to invite friends instantly |

---

## 🎮 How It Works

```
Host uploads PDF/PPTX
        ↓
Gemini AI generates questions privately (players can't see them)
        ↓
Host shares 6-character room code
        ↓
Friends join → Host clicks Start → 3-2-1 countdown
        ↓
  ┌─ Question shown with countdown timer
  │  Players type freely (fuzzy matching, acronyms accepted)
  │  Correct answer locks in with speed-based score (500–1000 pts)
  │  Wrong answer? Keep trying until timer expires
  │  Timer ends → answer revealed → 5s leaderboard
  └─ Next question (automatic)
        ↓
Round ends → 10s results screen → Next round (automatic)
        ↓
Final results → 🏆 Winner announced
```

---

## 🛠 Tech Stack

| Layer | Technology |
|---|---|
| **Frontend** | React 18, Vite, Socket.IO client |
| **Backend** | Python 3.11, Flask 3, Flask-SocketIO |
| **AI** | Google Gemini (`gemini-3.5-flash`) via `google-genai` SDK |
| **Fuzzy Matching** | `rapidfuzz` (token-sort ratio + WRatio) |
| **Document Parsing** | PyMuPDF (PDF), python-pptx (PowerPoint) |
| **Database** | SQLite (game history) |
| **Deployment** | Vercel (frontend) + Render (backend) |

---

## 🚀 Local Setup

You need **Python 3.10+**, **Node.js 20+**, and a [Gemini API key](https://aistudio.google.com/apikey) (free).

Open **two terminals** from the project root:

### Terminal 1 — Backend

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Create .env from the example
Copy-Item .env.example .env
# Edit backend\.env and set GEMINI_API_KEY=your-key-here

python app.py
```

Backend runs at → **http://localhost:5000**

Verify: `http://localhost:5000/api/health` should return `{"ok": true, "ai_configured": true}`

### Terminal 2 — Frontend

```powershell
cd frontend
npm install
npm run dev
```

Frontend runs at → **http://localhost:5173**

---

## 🌍 Deployment (Render + Vercel)

Your project already includes `render.yaml` and `frontend/vercel.json`.

### Quick Steps

**1. Push to GitHub** (this repo)

**2. Deploy backend → [Render](https://render.com)**
- New Web Service → connect this repo
- Root directory: `backend`
- Build: `pip install -r requirements.txt`
- Start: `gunicorn --worker-class gthread --threads 100 --bind 0.0.0.0:$PORT app:app`
- Add env vars: `GEMINI_API_KEY`, `GEMINI_MODEL=gemini-3.5-flash`

**3. Deploy frontend → [Vercel](https://vercel.com)**
- Import repo → Root directory: `frontend`
- Add env var: `VITE_API_URL=https://your-api.onrender.com`

**4. Wire up CORS on Render**
- Set `FRONTEND_ORIGIN` and `SOCKET_CORS_ORIGINS` to your Vercel URL

> ⚠️ Render free tier sleeps after 15 min idle (30s cold start). Upgrade to Starter ($7/mo) for always-on.

---

## ⚙️ Configuration

### Environment Variables

**Backend** (`backend/.env` locally, Render dashboard in production):

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | Your Gemini API key |
| `GEMINI_MODEL` | No | Default: `gemini-3.5-flash` |
| `FRONTEND_ORIGIN` | Production | Your Vercel URL (for CORS) |
| `SOCKET_CORS_ORIGINS` | Production | Same Vercel URL |
| `DB_PATH` | No | SQLite path (default: `quizclash.db`) |
| `FLASK_DEBUG` | No | Set `1` for local dev only |

**Frontend** (`frontend/.env` locally, Vercel dashboard in production):

| Variable | Description |
|---|---|
| `VITE_API_URL` | Full backend URL, e.g. `https://your-api.onrender.com` |

> 🔒 **Never put `GEMINI_API_KEY` in a `VITE_` variable** — it would be exposed in the browser bundle.

### Game Timing (backend/app.py)

| Constant | Default | Description |
|---|---|---|
| `QUESTION_RESULTS_SECONDS` | 5 | Leaderboard display after each question |
| `ROUND_END_SECONDS` | 10 | Break between rounds |
| `GAME_START_COUNTDOWN` | 3 | 3-2-1 splash before first question |
| `TIER_SECONDS['flash']` | 10 | ⚡ Flash questions |
| `TIER_SECONDS['standard']` | 15 | Standard questions |
| `TIER_SECONDS['reasoning']` | 25 | 🧠 Reasoning questions |
| `TIER_SECONDS['calculation']` | 45 | 🧮 Math/calculation questions |

---

## 📡 API Reference

### REST

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | Service check — reports AI status and timing config |
| `/api/questions/generate` | POST | Upload file + count → returns opaque `upload_id` |
| `/api/rooms` | POST | Create room with `upload_id`, settings, and host nickname |

### Socket.IO Events

**Client → Server:**
```
join_game          { code, nickname }
host_start_game    { code, host_token }
submit_answer      { code, player_id, answer }
play_again         { code, host_token }
```

**Server → Client:**
```
joined_game        Full game state on join/reconnect
lobby_update       Player list updates
game_starting      3-2-1 countdown begins
question_started   New question + deadline timestamp + difficulty tier
answer_ack         { correct, points_earned, submitted, message }
question_result    Answer reveal + leaderboard + countdown_ends_at
round_results      Round summary + countdown to next round
game_over          Final leaderboard + winner
room_reset         Lobby reset after Play Again
```

All countdown payloads include an absolute `countdown_ends_at` Unix timestamp — every client computes the same remaining seconds regardless of network delay.

---

## 🧠 Scoring

```
score = round(500 + 500 × ((limit - elapsed) / limit))
```

- **Fast correct answer** → up to **1000 points**
- **Slow correct answer** → minimum **500 points**
- **Wrong / no answer** → **0 points**
- Elapsed time measured **server-side** at submission moment

---

## ⚠️ Limitations

- Legacy `.ppt` files are not supported — convert to `.pptx` first
- Scanned/image-only PDFs need OCR; only selectable text is read
- Room state is in-memory — restarts end active games
- SQLite history resets on Render free tier unless a persistent disk is mounted

---

## 📄 License

MIT — do whatever you want with it.

---

*Made with ⚡ by Abhinav Babu · Powered by Google Gemini AI*
