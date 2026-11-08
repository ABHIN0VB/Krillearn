# QuizClash architecture (MVP)

## Components
- **React + Vite**: host setup, nickname join flow, lobby, live question UI, timer, answer submission, leaderboard.
- **Flask REST API**: document extraction, AI question generation, room creation and health check.
- **Flask-SocketIO**: lobby updates, synchronized round start, answer submission, reveals and leaderboard broadcasts.
- **OpenAI API**: source-grounded short-answer question generation, called only by the backend.
- **SQLite**: completed-game summaries.

## Private question generation
`POST /api/questions/generate` accepts a PDF or PPTX and returns only a random `upload_id`, question count, generation mode and notice. The generated question set and answer keys stay in server memory. `POST /api/rooms` consumes that one-time upload ID. The browser therefore does not receive a question preview or answer key before the room starts. The host participates as a player and receives the same live question event as other players; answers are revealed after the round closes.

## Room lifecycle
1. Host uploads a document and chooses the question count.
2. Backend extracts text and generates questions using OpenAI if configured. If no key is configured, it uses a clearly labelled heuristic fallback. If a configured AI call fails, it returns an error rather than silently falling back.
3. Backend stores generated questions under an opaque one-time upload ID and returns no question text.
4. Host creates a room using the upload ID and settings; backend returns room code and host token.
5. Host joins as a player, shares the code, and starts the game.
6. Server broadcasts each question with a deadline; answer correctness and points are calculated server-side.
7. After all connected players submit or the timer expires, the server reveals the answer and updates scores.
8. Final results are written to SQLite.

## Deployment
The supported MVP deployment is **Vercel for the frontend + Render for the Flask-SocketIO backend**. Vercel's standard serverless functions are not suitable for this backend's persistent WebSocket room state. See `DEPLOYMENT.md`.

## Scaling and production hardening
Live rooms and pending question sets are currently in-memory, so deploy one backend instance and expect rooms to be lost on restart/sleep. For a public release, move live state to Redis, persist questions/answers in PostgreSQL, add room/player reconnect tokens, rate limiting, file scanning, stronger host authorization, upload-size limits, usage limits and monitoring.
