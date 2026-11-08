import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { io } from 'socket.io-client';
import './styles.css';

const API = (import.meta.env.VITE_API_URL || 'http://localhost:5000').replace(/\/$/, '');
const socket = io(API, { autoConnect: true, transports: ['websocket', 'polling'] });

// ── Medal definitions ────────────────────────────────────────────────────────
const MEDALS = ['🥇', '🥈', '🥉'];

// ── Rank change badge ────────────────────────────────────────────────────────
function RankChangeBadge({ change }) {
  if (!change || change === 0) return <span className="rank-change neutral">−</span>;
  if (change > 0) return <span className="rank-change up">↑{change}</span>;
  return <span className="rank-change down">↓{Math.abs(change)}</span>;
}

// ── Server-synchronized countdown hook ───────────────────────────────────────
function useServerCountdown(countdownEndsAt) {
  const [remaining, setRemaining] = useState(null);
  useEffect(() => {
    if (!countdownEndsAt) {
      setRemaining(null);
      return;
    }
    const tick = () => {
      const left = Math.max(0, countdownEndsAt - Date.now() / 1000);
      setRemaining(Math.ceil(left));
    };
    tick();
    const id = setInterval(tick, 100);
    return () => clearInterval(id);
  }, [countdownEndsAt]);
  return remaining;
}

// ── Leaderboard component ─────────────────────────────────────────────────────
function Leaderboard({
  players = [],
  currentPlayerId = null,
  showQuestionPoints = false,
  showRoundPoints = false,
  showRankChange = false,
  compact = false,
}) {
  return (
    <div className={`leaderboard ${compact ? 'compact' : ''}`}>
      {players.length ? (
        players.map((p, i) => {
          const rank = p.rank || i + 1;
          const isTop1 = rank === 1;
          const isTop2 = rank === 2;
          const isTop3 = rank === 3;
          const isMe = p.id === currentPlayerId;

          return (
            <div
              key={p.id || p.nickname}
              className={[
                'leader-row',
                isTop1 ? 'first-place' : '',
                isTop2 ? 'second-place' : '',
                isTop3 ? 'third-place' : '',
                isMe ? 'my-row' : '',
              ].filter(Boolean).join(' ')}
            >
              <span className="leader-rank">
                {rank <= 3 ? MEDALS[rank - 1] : String(rank).padStart(2, '0')}
              </span>

              {showRankChange && (
                <RankChangeBadge change={p.rank_change} />
              )}

              <span className="avatar">
                {(p.nickname || '?').slice(0, 1).toUpperCase()}
              </span>

              <span className="leader-name">
                {p.nickname}
                {p.connected === false && <small> (disconnected)</small>}
                {isMe && <span className="you-pill">YOU</span>}
              </span>

              {showQuestionPoints && p.last_question_points > 0 && (
                <span className="points-badge question-pts">+{p.last_question_points}</span>
              )}

              {showRoundPoints && p.round_points > 0 && (
                <span className="points-badge round-pts">+{p.round_points} round</span>
              )}

              <strong>
                {p.score} <small>PTS</small>
              </strong>
            </div>
          );
        })
      ) : (
        <div className="empty-players">
          No players yet.<br />Invite friends with the room code.
        </div>
      )}
    </div>
  );
}

// ── Feature card ─────────────────────────────────────────────────────────────
function Feature({ icon, title, text }) {
  return (
    <div className="feature-card">
      <span className="feature-icon">{icon}</span>
      <h3>{title}</h3>
      <p>{text}</p>
    </div>
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
function App() {
  // Navigation
  const [mode, setMode] = useState('home');

  // Setup state
  const [nickname, setNickname] = useState('');
  const [joinCode, setJoinCode] = useState('');
  const [file, setFile] = useState(null);
  const [questionCount, setQuestionCount] = useState(10);
  const [difficulty, setDifficulty] = useState('mixed');
  const [seconds, setSeconds] = useState(15);
  const [questionsPerRound, setQuestionsPerRound] = useState(5);
  const [generationMode, setGenerationMode] = useState('');
  const [uploadId, setUploadId] = useState(null);
  const [generatedCount, setGeneratedCount] = useState(0);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  // Room / game state
  const [room, setRoom] = useState(null);
  const [playerId, setPlayerId] = useState(null);
  const [players, setPlayers] = useState([]);
  const [gameStatus, setGameStatus] = useState('lobby'); // lobby, starting, question, question_results, round_results, final_results
  const [question, setQuestion] = useState(null);
  const [answer, setAnswer] = useState('');
  const [answerSent, setAnswerSent] = useState(false);
  const [attemptError, setAttemptError] = useState(''); // Multi-attempt retry message
  const [ack, setAck] = useState(null);
  const [reveal, setReveal] = useState(null);           // question_result payload
  const [roundResults, setRoundResults] = useState(null); // round_results payload
  const [finalResult, setFinalResult] = useState(null); // game_over payload
  const [startCountdown, setStartCountdown] = useState(null); // 3-2-1 get-ready splash
  const [countdownEndsAt, setCountdownEndsAt] = useState(null); // server-side absolute timestamp
  const [now, setNow] = useState(Date.now());

  const fileRef = useRef(null);
  const answerRef = useRef(null);
  const hostToken = room?.hostToken;
  const isHost = Boolean(room?.hostToken);

  // Live clock for active question progress bar
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(id);
  }, []);

  // Server countdown hook
  const serverCountdown = useServerCountdown(countdownEndsAt);

  // Remaining seconds for current active question
  const timeLeft = useMemo(
    () => (question?.deadline ? Math.max(0, (question.deadline * 1000 - now) / 1000) : 0),
    [question, now]
  );

  // Reset answer input when question changes
  useEffect(() => {
    if (gameStatus === 'question') {
      setAnswerSent(false);
      setAnswer('');
      setAck(null);
      setAttemptError('');
    }
  }, [question?.index, gameStatus]);

  // ── Socket.IO Event Listeners ──────────────────────────────────────────────
  useEffect(() => {
    const onJoined = (state) => {
      setRoom((prev) => ({
        ...(prev || {}),
        code: state.code,
        hostNickname: state.host_nickname,
        settings: state.settings,
      }));
      setPlayerId(state.player_id);
      setPlayers(state.players || []);
      setGameStatus(state.status || 'lobby');
      setCountdownEndsAt(state.countdown_ends_at || null);

      if (state.question) setQuestion(state.question);
      if (state.reveal) setReveal(state.reveal);
      if (state.round_results) setRoundResults(state.round_results);
      if (state.final_payload) setFinalResult(state.final_payload);

      setMode('game');
      setError('');
    };

    const onJoinError = (d) => {
      setError(d.message || 'Could not join room.');
      setBusy(false);
    };

    const onLobby = (d) => {
      setPlayers(d.players || []);
      setGameStatus((prev) => d.status || prev);
    };

    const onStarting = (d) => {
      setStartCountdown(d.countdown_seconds ?? 3);
      setGameStatus('starting');
      setReveal(null);
      setRoundResults(null);
      setFinalResult(null);
      setAck(null);
      setCountdownEndsAt(null);
    };

    const onQuestion = (q) => {
      setQuestion(q);
      setAnswer('');
      setAck(null);
      setReveal(null);
      setRoundResults(null);
      setStartCountdown(null);
      setCountdownEndsAt(null);
      setGameStatus('question');
      setTimeout(() => answerRef.current?.focus(), 60);
    };

    const onQuestionResult = (d) => {
      setReveal(d);
      setPlayers(d.players || []);
      setCountdownEndsAt(d.countdown_ends_at || null);
      setGameStatus('question_results');
      setAck(null);
    };

    const onRoundResults = (d) => {
      setRoundResults(d);
      setPlayers(d.players || []);
      setCountdownEndsAt(d.countdown_ends_at || null);
      setGameStatus('round_results');
      setReveal(null);
    };

    const onLeaderboard = (d) => {
      setPlayers(d.players || []);
    };

    const onGameOver = (d) => {
      setFinalResult(d);
      setPlayers(d.players || []);
      setGameStatus('final_results');
      setReveal(null);
      setRoundResults(null);
      setQuestion(null);
      setCountdownEndsAt(null);
    };

    const onRoomReset = (d) => {
      setGameStatus('lobby');
      setPlayers(d.players || []);
      setQuestion(null);
      setReveal(null);
      setRoundResults(null);
      setFinalResult(null);
      setCountdownEndsAt(null);
      setAnswer('');
      setAnswerSent(false);
      setAck(null);
    };

    const onAck = (d) => {
      setAck(d);
      if (d.accepted && d.correct) {
        setAnswerSent(true);
        setAttemptError('');
      } else if (d.accepted && !d.correct) {
        // Wrong attempt: keep input enabled, clear text, show retry warning
        setAnswerSent(false);
        setAttemptError(d.message || 'No match — try again!');
        setAnswer('');
        setTimeout(() => answerRef.current?.focus(), 50);
      }
    };

    const onActionError = (d) => {
      setError(d.message || 'That action could not be completed.');
    };

    socket.on('joined_game', onJoined);
    socket.on('join_error', onJoinError);
    socket.on('lobby_update', onLobby);
    socket.on('game_starting', onStarting);
    socket.on('question_started', onQuestion);
    socket.on('question_result', onQuestionResult);
    socket.on('round_results', onRoundResults);
    socket.on('leaderboard_update', onLeaderboard);
    socket.on('game_over', onGameOver);
    socket.on('room_reset', onRoomReset);
    socket.on('answer_ack', onAck);
    socket.on('action_error', onActionError);

    return () => {
      socket.off('joined_game', onJoined);
      socket.off('join_error', onJoinError);
      socket.off('lobby_update', onLobby);
      socket.off('game_starting', onStarting);
      socket.off('question_started', onQuestion);
      socket.off('question_result', onQuestionResult);
      socket.off('round_results', onRoundResults);
      socket.off('leaderboard_update', onLeaderboard);
      socket.off('game_over', onGameOver);
      socket.off('room_reset', onRoomReset);
      socket.off('answer_ack', onAck);
      socket.off('action_error', onActionError);
    };
  }, []);

  // 3-2-1 get-ready countdown ticker
  useEffect(() => {
    if (gameStatus !== 'starting' || startCountdown === null) return;
    if (startCountdown <= 0) return;
    const id = setTimeout(() => setStartCountdown((c) => Math.max(0, c - 1)), 1000);
    return () => clearTimeout(id);
  }, [startCountdown, gameStatus]);

  // ── User Actions ──────────────────────────────────────────────────────────
  async function generateQuestions(e) {
    e.preventDefault();
    setError('');
    setNotice('');
    if (!file) {
      setError('Choose a PDF or PPTX file first.');
      return;
    }
    setBusy(true);
    try {
      const body = new FormData();
      body.append('file', file);
      body.append('question_count', String(questionCount));
      body.append('difficulty', difficulty);
      const response = await fetch(`${API}/api/questions/generate`, { method: 'POST', body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Question generation failed.');
      setUploadId(data.upload_id);
      setGeneratedCount(data.count);
      setGenerationMode(data.generation_mode);
      setNotice(data.notice || `${data.count} questions prepared privately on the server.`);
      setMode('review');
    } catch (err) {
      setError(err.message || 'Could not connect to the backend.');
    } finally {
      setBusy(false);
    }
  }

  async function createRoom() {
    setError('');
    setBusy(true);
    try {
      const response = await fetch(`${API}/api/rooms`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          upload_id: uploadId,
          question_count: Math.min(Number(questionCount), generatedCount),
          seconds_per_question: Number(seconds),
          questions_per_round: Number(questionsPerRound),
          host_nickname: nickname.trim() || 'Host',
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Could not create room.');
      setRoom({
        code: data.code,
        hostToken: data.host_token,
        hostNickname: nickname.trim() || 'Host',
        settings: data.settings,
      });
      setPlayerId(null);
      setPlayers([]);
      setGameStatus('lobby');
      setMode('game');
      setError('');
      socket.emit('join_game', { code: data.code, nickname: nickname.trim() || 'Host' });
    } catch (err) {
      setError(err.message || 'Could not create room.');
    } finally {
      setBusy(false);
    }
  }

  function joinRoom(e) {
    e.preventDefault();
    setError('');
    setBusy(true);
    socket.emit('join_game', { code: joinCode.trim().toUpperCase(), nickname: nickname.trim() });
  }

  function startGame() {
    setError('');
    socket.emit('host_start_game', { code: room.code, host_token: hostToken });
  }

  function submitAnswer(e) {
    e?.preventDefault();
    if (!answer.trim() || answerSent || gameStatus !== 'question') return;
    socket.emit('submit_answer', { code: room.code, player_id: playerId, answer: answer.trim() });
  }

  function playAgain() {
    if (isHost && room?.code && hostToken) {
      socket.emit('play_again', { code: room.code, host_token: hostToken });
    } else {
      reset();
    }
  }

  function reset() {
    setMode('home');
    setRoom(null);
    setPlayerId(null);
    setPlayers([]);
    setUploadId(null);
    setGeneratedCount(0);
    setQuestion(null);
    setReveal(null);
    setRoundResults(null);
    setFinalResult(null);
    setAck(null);
    setAttemptError('');
    setAnswer('');
    setAnswerSent(false);
    setError('');
    setNotice('');
    setGameStatus('lobby');
    setStartCountdown(null);
    setCountdownEndsAt(null);
    setFile(null);
  }

  const settingsSeconds = question?.seconds || room?.settings?.seconds_per_question || seconds;

  // ── JSX Render ────────────────────────────────────────────────────────────
  return (
    <div className="app-shell">
      <header className="topbar">
        <button className="brand" onClick={reset}>
          <span className="brand-mark">Q</span>
          <span>
            quiz<span className="brand-accent">zedup</span>
          </span>
        </button>
        <div className="top-meta">
          <span className="live-dot" /> REAL-TIME STUDY ARENA
        </div>
      </header>

      <main className="main-content">
        {error && (
          <div className="alert error">
            <span>⚠</span>
            {error}
            <button onClick={() => setError('')} aria-label="Dismiss">
              ×
            </button>
          </div>
        )}

        {/* ── HOME SCREEN ────────────────────────────────────────── */}
        {mode === 'home' && (
          <>
            <section className="hero">
              <div className="eyebrow">
                <span className="eyebrow-line" /> YOUR NOTES. YOUR FRIENDS. YOUR GLORY.
              </div>
              <h1>
                Study hard.<br />
                <span className="gradient-text">Answer faster.</span>
              </h1>
              <p className="hero-copy">
                Turn your lecture PDFs and slides into a live quiz battle. Invite friends, race the clock, and climb the live leaderboard with automatic round flow.
              </p>
              <div className="hero-actions">
                <button
                  className="button primary big"
                  onClick={() => {
                    setMode('create');
                    setError('');
                  }}
                >
                  ＋ Create a room
                </button>
                <button
                  className="button secondary big"
                  onClick={() => {
                    setMode('join');
                    setError('');
                  }}
                >
                  ↗ Join with code
                </button>
              </div>
              <div className="hero-stats">
                <div>
                  <strong>01</strong>
                  <span>Upload notes</span>
                </div>
                <i />
                <div>
                  <strong>02</strong>
                  <span>Invite friends</span>
                </div>
                <i />
                <div>
                  <strong>03</strong>
                  <span>Battle it out</span>
                </div>
              </div>
            </section>

            <section className="feature-grid">
              <Feature
                icon="✦"
                title="Gemini AI Questions"
                text="High-quality short-answer questions generated directly from your uploaded slides and notes."
              />
              <Feature
                icon="⚡"
                title="Automatic Round Flow"
                text="The game advances automatically after each question and round. Zero host clicking needed."
              />
              <Feature
                icon="🏆"
                title="Live Leaderboard"
                text="Rankings, gold/silver/bronze medals, and points reveal instantly after every question."
              />
            </section>
          </>
        )}

        {/* ── CREATE ROOM ────────────────────────────────────────── */}
        {mode === 'create' && (
          <section className="panel create-panel">
            <div className="panel-heading">
              <div>
                <div className="eyebrow">HOST SETUP</div>
                <h2>Create your quiz room</h2>
                <p>Upload your lecture material, then configure rounds and timing.</p>
              </div>
              <span className="step-pill">STEP 1 / 2</span>
            </div>

            <form onSubmit={generateQuestions}>
              <label className="field-label">Your nickname</label>
              <input
                className="text-input"
                maxLength="24"
                placeholder="e.g. Abhi"
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
              />

              <label className="field-label">Lecture PDF or PowerPoint (.pptx)</label>
              <button
                type="button"
                className={`upload-zone ${file ? 'has-file' : ''}`}
                onClick={() => fileRef.current?.click()}
              >
                <input
                  ref={fileRef}
                  type="file"
                  accept=".pdf,.pptx,.ppt"
                  hidden
                  onChange={(e) => setFile(e.target.files?.[0] || null)}
                />
                <span className="upload-icon">↑</span>
                <strong>{file ? file.name : 'Drop your file into the arena'}</strong>
                <span>
                  {file
                    ? `${(file.size / 1024 / 1024).toFixed(2)} MB · Selected`
                    : 'or click to browse · PDF / PPTX · Max 20 MB'}
                </span>
              </button>

              <div className="settings-grid">
                <div>
                  <label className="field-label">Questions to generate</label>
                  <select
                    className="text-input"
                    value={questionCount}
                    onChange={(e) => setQuestionCount(Number(e.target.value))}
                  >
                    {[5, 10, 15, 20, 25, 30].map((n) => (
                      <option key={n} value={n}>
                        {n} questions
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="field-label">Question difficulty</label>
                  <select
                    className="text-input"
                    value={difficulty}
                    onChange={(e) => setDifficulty(e.target.value)}
                  >
                    <option value="mixed">Mixed (Balanced)</option>
                    <option value="easy">Easy (Rapid recall)</option>
                    <option value="medium">Medium (Standard)</option>
                    <option value="hard">Hard (Deep reasoning)</option>
                    <option value="numerical">Numerical (Math & calculations)</option>
                  </select>
                </div>
              </div>

              <div className="settings-grid">
                <div>
                  <label className="field-label">Default fallback timer</label>
                  <select
                    className="text-input"
                    value={seconds}
                    onChange={(e) => setSeconds(Number(e.target.value))}
                  >
                    {[5, 10, 15, 20, 30, 45, 60].map((n) => (
                      <option key={n} value={n}>
                        {n} seconds
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="field-label">Questions per round</label>
                  <select
                    className="text-input"
                    value={questionsPerRound}
                    onChange={(e) => setQuestionsPerRound(Number(e.target.value))}
                  >
                    {[3, 5, 10].map((n) => (
                      <option key={n} value={n}>
                        {n} questions per round
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              <button className="button primary full" type="submit" disabled={busy}>
                {busy ? (
                  <>
                    <span className="spinner" /> Analyzing document with Gemini…
                  </>
                ) : (
                  '✦ Generate questions'
                )}
              </button>
              <p className="microcopy">
                Questions are generated from extracted document text using Google Gemini AI.
              </p>
            </form>
          </section>
        )}

        {/* ── REVIEW QUESTIONS READY ─────────────────────────────── */}
        {mode === 'review' && (
          <section className="panel private-ready-panel">
            <div className="private-ready-icon">✦</div>
            <div className="eyebrow centered">AI QUESTION ENGINE</div>
            <h2>Your questions are ready.</h2>
            <p className="panel-subtitle">
              We have prepared {generatedCount} questions and kept their text and answers secure on the server.
              Questions remain hidden until the live game begins.
            </p>

            {generationMode === 'fallback' && (
              <div className="alert warning">
                Gemini API is not configured on the backend yet. These are heuristic draft questions.
                Configure <code>GEMINI_API_KEY</code> in <code>backend/.env</code> for full AI generation.
              </div>
            )}

            {notice && <p className="notice-text">{notice}</p>}

            <div className="private-ready-stats">
              <span>🔒 Server-side questions</span>
              <span>✦ {generatedCount} questions ready</span>
              <span>◷ {seconds}s per question</span>
              <span>⚡ {questionsPerRound} questions/round</span>
            </div>

            <div className="review-actions">
              <button
                className="button secondary"
                onClick={() => {
                  setMode('create');
                  setUploadId(null);
                }}
              >
                ← Back
              </button>
              <button
                className="button primary"
                disabled={busy || !uploadId}
                onClick={createRoom}
              >
                {busy ? 'Creating…' : 'Create room & get code →'}
              </button>
            </div>
          </section>
        )}

        {/* ── JOIN ROOM ──────────────────────────────────────────── */}
        {mode === 'join' && (
          <section className="panel join-panel">
            <div className="join-emblem">↗</div>
            <div className="eyebrow centered">YOUR FRIENDS ARE WAITING</div>
            <h2>Jump into a room</h2>
            <p className="panel-subtitle">Enter the 6-character code shared by your host.</p>

            <form onSubmit={joinRoom}>
              <label className="field-label">Your nickname</label>
              <input
                className="text-input"
                maxLength="24"
                placeholder="Pick a name"
                required
                value={nickname}
                onChange={(e) => setNickname(e.target.value)}
              />

              <label className="field-label">Room code</label>
              <input
                className="text-input code-input"
                maxLength="6"
                placeholder="A7K9PX"
                required
                value={joinCode}
                onChange={(e) =>
                  setJoinCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ''))
                }
              />

              <button
                className="button primary full"
                disabled={busy || !nickname.trim() || joinCode.length !== 6}
              >
                {busy ? 'Joining room…' : 'Join the battle →'}
              </button>
            </form>

            <button className="text-button" onClick={() => setMode('home')}>
              ← Back to home
            </button>
          </section>
        )}

        {/* ── ACTIVE GAME ARENA ───────────────────────────────────── */}
        {mode === 'game' && room && (
          <section className="game-layout">
            <div className="game-main">
              {/* Header bar */}
              <div className="game-topline">
                <div>
                  <div className="eyebrow">ROOM CODE</div>
                  <button
                    className="room-code"
                    onClick={() => navigator.clipboard?.writeText(room.code)}
                    title="Copy room code"
                  >
                    {room.code} <span>▢</span>
                  </button>
                </div>

                <div className="room-status">
                  <span className="live-dot" />
                  {gameStatus === 'lobby'
                    ? 'LOBBY OPEN'
                    : gameStatus === 'starting'
                    ? 'GET READY'
                    : gameStatus === 'question'
                    ? 'LIVE QUESTION'
                    : gameStatus === 'question_results'
                    ? 'QUESTION LEADERBOARD'
                    : gameStatus === 'round_results'
                    ? 'ROUND RESULTS'
                    : gameStatus === 'final_results'
                    ? 'FINAL RESULTS'
                    : 'PLAYING'}
                </div>
              </div>

              {/* Progress Indicator */}
              {question && (gameStatus === 'question' || gameStatus === 'question_results') && (
                <div className="progress-bar-wrap">
                  <span className="progress-pill">
                    Round {question.round} of {question.total_rounds} · Question{' '}
                    {question.question_in_round || question.index + 1} of{' '}
                    {question.total_in_round || question.total}
                  </span>
                </div>
              )}

              {/* ── 1. LOBBY SCREEN ── */}
              {gameStatus === 'lobby' && (
                <div className="lobby-content">
                  <div className="lobby-graphic">⚡</div>
                  <h2>Room is ready.</h2>
                  <p>Share the code with your crew. The battle begins when the host starts.</p>

                  <div
                    className="share-code"
                    onClick={() => navigator.clipboard?.writeText(room.code)}
                  >
                    <span>{room.code}</span>
                    <button className="button secondary small">Copy code</button>
                  </div>

                  <div className="lobby-settings">
                    <span>◷ {room.settings?.seconds_per_question || seconds}s per question</span>
                    <span>▤ {room.settings?.question_count || generatedCount} questions</span>
                    <span>⚡ Auto-advancement enabled</span>
                  </div>

                  {isHost ? (
                    <button
                      className="button primary big full"
                      disabled={!players.length}
                      onClick={startGame}
                    >
                      Start game →
                    </button>
                  ) : (
                    <div className="waiting-note">
                      <span className="spinner" /> Waiting for the host to start…
                    </div>
                  )}
                </div>
              )}

              {/* ── 2. GET READY COUNTDOWN (3-2-1) ── */}
              {gameStatus === 'starting' && (
                <div className="waiting-screen">
                  <div className="countdown-number">
                    {startCountdown > 0 ? startCountdown : 'GO!'}
                  </div>
                  <h2>Get ready!</h2>
                  <p>First question incoming in moments.</p>
                </div>
              )}

              {/* ── 3. QUESTION ACTIVE ── */}
              {gameStatus === 'question' && question && (
                <div className="question-live">
                  <div className="question-meta">
                    <span className="question-round-badge">
                      ROUND {question.round} · QUESTION {question.question_in_round || question.index + 1}{' '}
                      <i>/</i> {question.total_in_round || question.total}
                    </span>
                    <div className="question-tier-badges">
                      {question.difficulty && (
                        <span className={`difficulty-tag tag-${question.difficulty}`}>
                          {question.difficulty.toUpperCase()}
                        </span>
                      )}
                      {question.time_tier && (
                        <span className="tier-tag">
                          {question.time_tier === 'flash' && '⚡ Flash 10s'}
                          {question.time_tier === 'standard' && '⏱ Standard 15s'}
                          {question.time_tier === 'reasoning' && '🧠 Reasoning 25s'}
                          {question.time_tier === 'calculation' && '🧮 Math/Calc 45s'}
                        </span>
                      )}
                    </div>
                    <span className={timeLeft < 5 ? 'timer urgent' : 'timer'}>
                      ◷ {Math.ceil(timeLeft)}s
                    </span>
                  </div>

                  <div className="timer-track">
                    <div
                      className="timer-fill"
                      style={{
                        width: `${Math.max(
                          0,
                          Math.min(100, (timeLeft / settingsSeconds) * 100)
                        )}%`,
                      }}
                    />
                  </div>

                  <h2 className="live-question-text">{question.question}</h2>

                  <p className="answer-hint">
                    {isHost
                      ? 'You are the host, but you can answer too! Rapid typing and typos are supported.'
                      : 'Type your answer and press Enter. Typos and acronyms are accepted!'}
                  </p>

                  <form className="answer-form" onSubmit={submitAnswer}>
                    <input
                      ref={answerRef}
                      className={`answer-input ${attemptError ? 'shake' : ''}`}
                      autoComplete="off"
                      disabled={answerSent || timeLeft <= 0}
                      placeholder={
                        answerSent
                          ? '✓ Answer locked in!'
                          : attemptError
                          ? 'Try again…'
                          : 'Type your answer…'
                      }
                      value={answer}
                      onChange={(e) => {
                        setAnswer(e.target.value);
                        if (attemptError) setAttemptError('');
                      }}
                    />
                    <button
                      className="button primary"
                      disabled={answerSent || !answer.trim() || timeLeft <= 0}
                    >
                      {answerSent ? 'Locked In' : 'Submit ↗'}
                    </button>
                  </form>

                  {attemptError && !answerSent && (
                    <div className="retry-warning">
                      <span>⚠</span> {attemptError}
                    </div>
                  )}

                  {answerSent && ack && ack.correct && (
                    <div className="submitted-note correct">
                      ✓ Correct! +{ack.points_earned} points locked in
                    </div>
                  )}

                  {!answerSent && !attemptError && (
                    <div className="live-tip">
                      ⚡ Type as many attempts as needed until the clock expires!
                    </div>
                  )}
                </div>
              )}

              {/* ── 4. QUESTION RESULTS / LEADERBOARD (5s Countdown) ── */}
              {gameStatus === 'question_results' && reveal && (
                <div className="reveal-content">
                  <div className="reveal-icon">✦</div>
                  <div className="eyebrow centered">CORRECT ANSWER</div>
                  <h2 className="reveal-answer">{reveal.answer}</h2>

                  {reveal.explanation && (
                    <p className="reveal-explanation">{reveal.explanation}</p>
                  )}

                  <span className="source-tag">⌁ {reveal.source || 'Uploaded notes'}</span>

                  {/* 5-second synchronized countdown banner */}
                  <div className="results-countdown-wrap">
                    <span className="results-countdown-label">
                      {reveal.is_last_in_round
                        ? reveal.is_last_round
                          ? 'Final results in'
                          : 'Round results in'
                        : 'Next question in'}
                    </span>
                    <span className="results-countdown-number">
                      {serverCountdown ?? '…'}s
                    </span>
                  </div>

                  {/* Question Results: Answers submitted */}
                  <div className="round-results">
                    <h3>Submissions for this question</h3>
                    {reveal.results?.map((r, i) => (
                      <div
                        className={`result-row ${r.player_id === playerId ? 'my-result' : ''}`}
                        key={r.player_id}
                      >
                        <span className="rank">{i + 1}</span>
                        <span className="result-name">
                          {r.nickname}
                          <small>
                            {r.correct
                              ? `Correct in ${r.elapsed}s`
                              : r.answer
                              ? `Answered: "${r.answer}"`
                              : 'No answer submitted'}
                          </small>
                        </span>
                        <strong className={r.points_earned ? 'points-positive' : ''}>
                          +{r.points_earned}
                        </strong>
                      </div>
                    ))}
                  </div>

                  {/* Live Leaderboard after question */}
                  <div className="leaderboard-section">
                    <h3>Current Rankings</h3>
                    <Leaderboard
                      players={reveal.players || players}
                      currentPlayerId={playerId}
                      showQuestionPoints
                      showRankChange
                    />
                  </div>
                </div>
              )}

              {/* ── 5. ROUND-END LEADERBOARD (10s Countdown) ── */}
              {gameStatus === 'round_results' && roundResults && (
                <div className="round-end-content">
                  <div className="round-end-emblem">⚡</div>
                  <div className="eyebrow centered">ROUND COMPLETE</div>
                  <h2>{roundResults.message || `Round ${roundResults.round} Complete!`}</h2>
                  <p className="panel-subtitle">
                    Great hustle! Review the round standings below. Next round begins automatically.
                  </p>

                  {/* 10-second round countdown */}
                  <div className="round-countdown-box">
                    <span className="round-countdown-text">Next round starts in</span>
                    <span className="round-countdown-value">
                      {serverCountdown ?? 10}
                    </span>
                    <span className="round-countdown-sub">seconds…</span>
                  </div>

                  <div className="leaderboard-section">
                    <h3>Round {roundResults.round} Standings</h3>
                    <Leaderboard
                      players={roundResults.players || players}
                      currentPlayerId={playerId}
                      showRoundPoints
                      showRankChange
                    />
                  </div>
                </div>
              )}

              {/* ── 6. FINAL RESULTS (Game Over) ── */}
              {gameStatus === 'final_results' && (
                <div className="final-content">
                  <div className="trophy-wrap">
                    <div className="trophy-anim">🏆</div>
                  </div>

                  <div className="eyebrow centered">TOURNAMENT COMPLETE</div>
                  <h2>Final Standings</h2>

                  {finalResult?.winner && (
                    <div className="winner-banner">
                      <span className="winner-label">CHAMPION</span>
                      <strong className="winner-name">{finalResult.winner.nickname}</strong>
                      <span className="winner-score">
                        {finalResult.winner.score} total points
                      </span>
                    </div>
                  )}

                  <div className="leaderboard-section">
                    <h3>Final Leaderboard</h3>
                    <Leaderboard
                      players={finalResult?.players || players}
                      currentPlayerId={playerId}
                    />
                  </div>

                  <div className="final-actions">
                    {isHost ? (
                      <button className="button primary full big" onClick={playAgain}>
                        ↺ Play Again with Same Room
                      </button>
                    ) : (
                      <button className="button primary full" onClick={reset}>
                        ← Back to Home
                      </button>
                    )}
                    {isHost && (
                      <button className="text-button leave-button" onClick={reset}>
                        Leave Room & Return Home
                      </button>
                    )}
                  </div>
                </div>
              )}
            </div>

            {/* ── PERSISTENT SIDEBAR ─────────────────────────────────── */}
            <aside className="sidebar">
              <div className="sidebar-heading">
                <div>
                  <h3>Players</h3>
                  <p>{players.length} in the arena</p>
                </div>
                <span className="player-count">{players.length}</span>
              </div>

              <Leaderboard
                players={players}
                currentPlayerId={playerId}
                compact
              />

              <div className="sidebar-note">
                <span>✦</span>
                <p>
                  <strong>Automatic Flow:</strong>
                  <br />
                  Answers lock automatically, followed by a 5s question review and 10s round breaks.
                </p>
              </div>

              <button className="text-button leave-button" onClick={reset}>
                Leave room
              </button>
            </aside>
          </section>
        )}
      </main>

      <footer>
        <span>QUIZZEDUP © 2026</span>
        <span>POWERED BY GOOGLE GEMINI AI <b>✦</b></span>
      </footer>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
