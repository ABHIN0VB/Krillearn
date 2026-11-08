# QuizClash Deployment Guide: Vercel + Render + Google Gemini AI

## Architecture Overview

QuizClash is split into two cleanly separated tiers:
- **Frontend (Vercel)**: React + Vite single-page application.
- **Backend (Render)**: Persistent Python web service running Flask and Flask-SocketIO with WebSocket support.

> [!IMPORTANT]
> Do **not** deploy the backend as serverless functions on Vercel. QuizClash uses persistent WebSocket connections and in-memory rooms for synchronized real-time multiplayer gameplay and automatic countdown progression.

---

## 1. Deploy the Backend to Render

1. Push your repository to GitHub.
2. In the [Render Dashboard](https://dashboard.render.com), click **New → Web Service** and connect your repository.
3. Configure the service settings:
   - **Root Directory**: `backend`
   - **Environment**: `Python`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn --worker-class gthread --threads 100 --bind 0.0.0.0:$PORT app:app`
   - **Instance Type**: 1 instance (do not scale horizontally across multiple instances in the in-memory MVP)
4. Add the following **Environment Variables** in Render:
   - `GEMINI_API_KEY`: Your secret Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).
   - `GEMINI_MODEL`: `gemini-1.5-flash`
   - `FRONTEND_ORIGIN`: Your deployed Vercel URL, e.g. `https://your-quizclash.vercel.app` (set this after Step 2 below).
   - `SOCKET_CORS_ORIGINS`: Same Vercel URL, e.g. `https://your-quizclash.vercel.app`.
   - `DB_PATH`: `/var/data/quizclash.db` (if you attach a Render persistent disk; otherwise omit or leave default `quizclash.db`).
5. (Optional) For persistent game history across Render sleep/restarts, add a **Persistent Disk** mounted at `/var/data` (1 GB is plenty).
6. Click **Create Web Service**.
7. Once deployed, test the health check endpoint:
   ```
   https://YOUR-BACKEND.onrender.com/api/health
   ```
   It should return:
   ```json
   {
     "ok": true,
     "service": "QuizClash API",
     "ai_configured": true,
     "ai_provider": "gemini",
     "model": "gemini-1.5-flash",
     "timing": {
       "question_seconds": 15,
       "question_results_seconds": 5,
       "round_end_seconds": 10,
       "game_start_countdown": 3
     }
   }
   ```

---

## 2. Deploy the Frontend to Vercel

1. In the [Vercel Dashboard](https://vercel.com), click **Add New → Project** and import the same repository.
2. Configure the project:
   - **Root Directory**: `frontend`
   - **Framework Preset**: `Vite`
   - **Build Command**: `npm run build`
   - **Output Directory**: `dist`
3. Add the **Environment Variable**:
   - `VITE_API_URL`: Your full Render backend URL without trailing slash:
     ```
     https://YOUR-BACKEND.onrender.com
     ```
4. Click **Deploy**.
5. Once Vercel assigns your production domain (e.g. `https://quizclash-abc.vercel.app`), return to Render:
   - Update `FRONTEND_ORIGIN` to `https://quizclash-abc.vercel.app`
   - Update `SOCKET_CORS_ORIGINS` to `https://quizclash-abc.vercel.app`
   - Trigger a manual redeploy on Render so CORS settings take effect.

---

## 3. Production Environment Variable Checklist

### Render (Backend)
| Variable | Value | Secret? |
|---|---|---|
| `GEMINI_API_KEY` | `AIzaSy...` (from Google AI Studio) | **YES** — Never expose |
| `GEMINI_MODEL` | `gemini-1.5-flash` | No |
| `FRONTEND_ORIGIN` | `https://your-app.vercel.app` | No |
| `SOCKET_CORS_ORIGINS` | `https://your-app.vercel.app` | No |
| `DB_PATH` | `/var/data/quizclash.db` (with disk) | No |
| `FLASK_DEBUG` | `0` | No |

### Vercel (Frontend)
| Variable | Value | Secret? |
|---|---|---|
| `VITE_API_URL` | `https://your-backend.onrender.com` | Public backend URL |

> [!CAUTION]
> Never prefix `GEMINI_API_KEY` with `VITE_` or store it in the frontend repository or Vercel settings. All AI calls route securely through the Flask backend.

---

## 4. Verification and Testing

1. Open the deployed Vercel site in two browser windows (or one normal window and one incognito window).
2. Host:
   - Enter your nickname.
   - Upload a sample lecture PDF or PPTX.
   - Select question count (e.g. 10) and questions per round (e.g. 5).
   - Click **Generate questions**.
   - Click **Create room & get code**.
3. Player 2:
   - Enter nickname and the 6-character room code.
   - Click **Join the battle**.
4. Both players should appear live on the lobby scoreboard.
5. Host clicks **Start game**:
   - 3-second synchronized splash countdown appears.
   - Question 1 loads for both players simultaneously with countdown bar.
   - Both submit answers.
   - Question locks when timer expires.
   - Correct answer reveals with gold 🥇, silver 🥈, bronze 🥉 styling and current player highlighted.
   - 5-second countdown displays: `Next question in 5...4...3...`.
   - Next question automatically begins.
   - After question 5 (end of round 1): dedicated Round-End screen appears with 10-second countdown `Next round starts in 10...9...8...`.
   - Round 2 starts automatically without host clicks.
   - After the final question: Final results screen displays with winner trophy animation and "Play Again" button.
