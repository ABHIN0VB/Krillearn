import os, re, json, time, random, secrets, sqlite3, threading
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, join_room as socket_join_room, emit
from werkzeug.utils import secure_filename
from rapidfuzz import fuzz

BASE_DIR = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / '.env')
except ImportError:
    pass

UPLOAD_DIR = BASE_DIR / 'uploads'
UPLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = os.getenv('DB_PATH', str(BASE_DIR / 'quizclash.db'))
ALLOWED_EXTENSIONS = {'.pdf', '.pptx'}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_QUESTIONS = 50

# ── Central Timing Configuration (Authoritative Server Values) ───────────────
DEFAULT_QUESTION_SECONDS = 15        # Time allowed to answer each question
QUESTION_RESULTS_SECONDS = 5         # Leaderboard display time after each question
ROUND_END_SECONDS = 10               # Leaderboard display time after each round
GAME_START_COUNTDOWN = 3             # 3-2-1 get-ready splash before first question
DEFAULT_QUESTIONS_PER_ROUND = 5      # Number of questions per round

# Tiered timing mapping
TIER_SECONDS = {
    'flash': 10,
    'standard': 15,
    'reasoning': 25,
    'calculation': 45
}
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES

def configured_origins(name):
    raw = os.getenv(name, '*').strip()
    return '*' if raw == '*' else [origin.strip() for origin in raw.split(',') if origin.strip()]

CORS(app, resources={r'/api/*': {'origins': configured_origins('FRONTEND_ORIGIN')}}, supports_credentials=False)
socketio = SocketIO(app, cors_allowed_origins=configured_origins('SOCKET_CORS_ORIGINS'), async_mode='threading')

# Active rooms in memory. Completed game summaries are persisted in SQLite.
rooms = {}
pending_question_sets = {}  # short-lived upload IDs
rooms_lock = threading.RLock()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT, room_code TEXT NOT NULL,
            created_at TEXT NOT NULL, ended_at TEXT, question_count INTEGER NOT NULL,
            results_json TEXT NOT NULL
        )''')

init_db()


def json_error(message, status=400):
    return jsonify({'error': message}), status


def normalize_answer(value):
    value = str(value or '').strip().lower()
    value = value.replace('−', '-').replace('\u2019', "'")
    value = re.sub(r'^[\s"' + "'" + r'`]+|[\s"' + "'" + r'`.,!?;:]+$', '', value)
    value = re.sub(r'\s+', ' ', value)
    return value


def generate_acronyms(phrase):
    """Generate acronym for phrases with 2+ alphanumeric words (e.g. 'Greedy best first search' -> 'gbfs')."""
    words = [w for w in re.split(r'[\s\-_/]+', phrase) if w]
    acrs = []
    if len(words) >= 2:
        acr = ''.join(w[0] for w in words if w[0].isalnum()).lower()
        if len(acr) >= 2:
            acrs.append(acr)
    return acrs


def is_answer_correct(user_answer, target_answer, accepted_list=None):
    """
    Robust answer verification:
    1. Exact match (case & punctuation insensitive)
    2. Acronym match (e.g. 'GBFS' for 'Greedy best first search')
    3. Fuzzy match: token_sort_ratio >= 82% or WRatio >= 84% for multi-word phrases and typo tolerance
    """
    norm_user = normalize_answer(user_answer)
    if not norm_user:
        return False

    all_targets = [target_answer] + list(accepted_list or [])
    all_normalized = []
    for t in all_targets:
        nt = normalize_answer(t)
        if nt:
            all_normalized.append(nt)
            for acr in generate_acronyms(t):
                all_normalized.append(acr)
    all_normalized = list(dict.fromkeys(all_normalized))

    # 1. Exact match against target or acronym
    if norm_user in all_normalized:
        return True

    # 2. Fuzzy match
    for target in all_normalized:
        if len(target) <= 3:
            continue
        if abs(len(norm_user) - len(target)) > max(6, len(target) * 0.4):
            continue
        ratio = fuzz.token_sort_ratio(norm_user, target)
        if ratio >= 82:
            return True
        if len(target) > 8:
            wr = fuzz.WRatio(norm_user, target)
            if wr >= 84:
                return True

    return False


def extract_pdf(path):
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            raise RuntimeError('PDF support is missing. Install requirements.txt first.')
    pages = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            text = page.get_text('text').strip()
            if text:
                pages.append({'location': f'Page {i + 1}', 'text': text})
    return pages


def extract_pptx(path):
    try:
        from pptx import Presentation
    except ImportError:
        raise RuntimeError('PowerPoint support is missing. Install requirements.txt first.')
    slides = []
    prs = Presentation(path)
    for i, slide in enumerate(prs.slides):
        chunks = []
        for shape in slide.shapes:
            if getattr(shape, 'has_text_frame', False):
                txt = shape.text.strip()
                if txt:
                    chunks.append(txt)
            if getattr(shape, 'has_table', False):
                for row in shape.table.rows:
                    chunks.append(' | '.join(cell.text.strip() for cell in row.cells if cell.text.strip()))
        text = '\n'.join(chunks).strip()
        if text:
            slides.append({'location': f'Slide {i + 1}', 'text': text})
    return slides


def extract_document(path):
    suffix = Path(path).suffix.lower()
    if suffix == '.pdf':
        return extract_pdf(path)
    if suffix == '.pptx':
        return extract_pptx(path)
    if suffix == '.ppt':
        raise RuntimeError('Legacy .ppt files are not supported yet. Save the presentation as .pptx and upload again.')
    raise RuntimeError('Upload a PDF or PowerPoint .pptx file.')


def llm_generate_questions(pages, count, difficulty='mixed'):
    """Generate questions using Google Gemini AI, supporting both google.genai and google.generativeai."""
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        return None

    preferred_model = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash')
    model_candidates = list(dict.fromkeys([
        preferred_model,
        'gemini-3.5-flash',
        'gemini-3.8-flash',
        'gemma-4-26b-a4b-it'
    ]))

    source = '\n\n'.join(f"[{p['location']}]\n{p['text']}" for p in pages)
    source = source[:50000]

    difficulty_instructions = {
        'easy': 'Focus on basic facts, key definitions, and straightforward terms.',
        'medium': 'Focus on core concepts, relationships between terms, and standard problem-solving.',
        'hard': 'Focus on deep conceptual reasoning, edge cases, subtle distinctions, and multi-step ideas.',
        'numerical': 'Include numerical calculation questions, formula evaluations, quantities, or step counts from the source where available.',
        'mixed': 'Include a balanced mix of easy definitions, conceptual questions, and reasoning/numerical questions if appropriate.'
    }.get(difficulty, 'Include a balanced mix of questions.')

    prompt = f'''Create exactly {count} high-priority, high-yield quiz questions from ONLY the source text below.
PRIORITY REQUIREMENT:
- Identify and extract the MOST IMPORTANT and fundamental concepts, key definitions, core formulas, essential mechanisms, and critical takeaways from the material.
- If the source material contains multiple-choice questions (MCQs), practice questions, review quizzes, or highlighted exercises, PRIORITIZE converting and including those exact high-importance questions into the rounds.
- Focus on concepts most likely to be tested on an exam or core to understanding the topic. Avoid trivial trivia, dates, slide header text, or minor parenthetical remarks.

DIFFICULTY GUIDELINE: {difficulty_instructions}

Each answer must be one word or a short technical term (roughly 1-4 words, or a number/numerical value). Questions must be unambiguous and answerable from the source. Do not create multiple-choice questions in the final output (provide direct short-answer target answers so players can type them).

For each question, assign:
- "difficulty": one of "easy", "medium", "hard", "numerical"
- "time_tier": assign based on thinking/solving effort needed:
    * "flash" (10s) - simple rapid recall / 1-word definition
    * "standard" (15s) - standard factual / terminology question
    * "reasoning" (25s) - multi-concept understanding or deduction
    * "calculation" (45s) - numerical calculations or formula application

Return ONLY valid JSON as an object with a "questions" key containing an array of objects with keys:
question, answer, accepted_answers (array of strings), explanation, source, difficulty, time_tier.

The source field must use a page/slide label present in the text. Avoid duplicate questions. For acronyms, include common expanded forms or acronym letters in accepted_answers where appropriate.

SOURCE:
{source}

Return ONLY valid JSON with no extraneous text.'''

    text = None
    last_exc = None

    for m in model_candidates:
        # Try modern google-genai SDK first
        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=m,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type='application/json'
                )
            )
            text = response.text.strip() if response and response.text else None
            if text:
                break
        except Exception as exc:
            last_exc = exc
            # Fallback to google.generativeai if available
            try:
                import google.generativeai as gai
                gai.configure(api_key=api_key)
                model = gai.GenerativeModel(m)
                response = model.generate_content(
                    prompt,
                    generation_config=gai.GenerationConfig(
                        temperature=0.2,
                        response_mime_type='application/json'
                    )
                )
                text = response.text.strip() if response and response.text else None
                if text:
                    break
            except Exception as exc2:
                last_exc = exc2

    if not text:
        app.logger.exception('Gemini question generation failed')
        raise RuntimeError(
            f'AI question generation failed ({type(last_exc).__name__ if last_exc else "EmptyResponse"}). '
            f'Check GEMINI_API_KEY, GEMINI_MODEL, API billing/quota, and network access, then try again.'
        ) from last_exc

    try:
        # Strip markdown code fences if present
        if text.startswith('```'):
            text = re.sub(r'^```[a-z]*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)

        payload = json.loads(text)
        raw_questions = payload.get('questions', []) if isinstance(payload, dict) else payload
        cleaned = []
        for q in raw_questions:
            question = str(q.get('question', '')).strip()
            answer = str(q.get('answer', '')).strip()
            if not question or not answer or len(answer) > 80:
                continue
            accepted = q.get('accepted_answers') or []
            if not isinstance(accepted, list):
                accepted = []
            accepted = [answer] + [str(x).strip() for x in accepted if str(x).strip()]

            # Acronym enrichment
            for acr in generate_acronyms(answer):
                if acr.upper() not in accepted:
                    accepted.append(acr.upper())

            # Difficulty & Time Tier
            tier = str(q.get('time_tier', 'standard')).strip().lower()
            if tier not in TIER_SECONDS:
                tier = 'standard'
            q_seconds = TIER_SECONDS[tier]

            diff = str(q.get('difficulty', 'medium')).strip().lower()
            if diff not in ('easy', 'medium', 'hard', 'numerical'):
                diff = 'medium'

            cleaned.append({
                'question': question,
                'answer': answer,
                'accepted_answers': list(dict.fromkeys(accepted)),
                'explanation': str(q.get('explanation', '')).strip(),
                'source': str(q.get('source', 'Uploaded document')).strip(),
                'difficulty': diff,
                'time_tier': tier,
                'seconds': q_seconds,
            })
        return cleaned[:count] if cleaned else None
    except Exception as exc:
        app.logger.exception('Failed to parse Gemini response JSON')
        raise RuntimeError(f'Could not parse AI response: {str(exc)}') from exc


def fallback_generate_questions(pages, count, difficulty='mixed'):
    """Heuristic fallback when no API key is configured."""
    candidates = []
    patterns = [
        re.compile(r'^\s*([A-Za-z][A-Za-z0-9 ()/+-]{1,55}?)\s+(?:is|are|refers to|means)\s+(.{12,180})[.!]?\s*$', re.I),
        re.compile(r'^\s*([A-Za-z][A-Za-z0-9 ()/+-]{1,45}?)\s*[:—-]\s*(.{15,180})\s*$')
    ]
    for page in pages:
        for line in re.split(r'[\n•]+', page['text']):
            line = re.sub(r'\s+', ' ', line).strip(' -\t')
            if len(line) < 22 or len(line) > 220:
                continue
            match = next((p.match(line) for p in patterns if p.match(line)), None)
            if not match:
                continue
            term = match.group(1).strip(' .,:;')
            definition = match.group(2).strip(' .,:;')
            if len(term.split()) > 5 or len(term) > 55:
                continue
            acc = [term]
            for acr in generate_acronyms(term):
                if acr.upper() not in acc:
                    acc.append(acr.upper())
            candidates.append({
                'question': f"Which term is described as: {definition}?",
                'answer': term,
                'accepted_answers': acc,
                'explanation': definition,
                'source': page['location'],
                'difficulty': 'medium',
                'time_tier': 'standard',
                'seconds': TIER_SECONDS['standard'],
            })
    unique = []
    seen = set()
    for q in candidates:
        key = (normalize_answer(q['question']), normalize_answer(q['answer']))
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique[:count]


def generate_questions(pages, count, difficulty='mixed'):
    if os.getenv('GEMINI_API_KEY'):
        questions = llm_generate_questions(pages, count, difficulty)
        if not questions:
            raise RuntimeError('The AI returned no valid questions. Try fewer questions or a clearer document.')
        return questions, 'ai'
    return fallback_generate_questions(pages, count, difficulty), 'fallback'


# ── Room State & Leaderboard Helpers ─────────────────────────────────────────

def calculate_ranks(room):
    """Sort players by total score, assign ranks (1-indexed) and calculate rank change."""
    sorted_players = sorted(
        room['players'].values(),
        key=lambda p: (-p['score'], p['nickname'].lower())
    )
    for i, p in enumerate(sorted_players):
        curr_rank = i + 1
        prev_rank = p.get('previous_rank', curr_rank)
        p['rank_change'] = prev_rank - curr_rank  # positive = moved up, negative = dropped
        p['current_rank'] = curr_rank


def public_players(room):
    calculate_ranks(room)
    return sorted([
        {
            'id': p['id'],
            'nickname': p['nickname'],
            'score': p['score'],
            'connected': p['connected'],
            'rank': p.get('current_rank', 1),
            'rank_change': p.get('rank_change', 0),
            'last_question_points': p.get('last_question_points', 0),
            'round_points': p.get('round_points', 0),
        }
        for p in room['players'].values()
    ], key=lambda p: p['rank'])


def get_round_info(room, question_index=None):
    """Compute round number and question number in round."""
    if question_index is None:
        question_index = room.get('question_index', 0)
    q_per_round = room['settings'].get('questions_per_round', DEFAULT_QUESTIONS_PER_ROUND)
    total_q = len(room['questions'])
    total_rounds = max(1, (total_q + q_per_round - 1) // q_per_round)

    curr_round = (question_index // q_per_round) + 1
    curr_round = min(curr_round, total_rounds)

    q_in_round = (question_index % q_per_round) + 1
    total_in_round = min(q_per_round, total_q - (curr_round - 1) * q_per_round)

    is_last_in_round = (q_in_round >= total_in_round) or (question_index >= total_q - 1)
    is_last_round = (curr_round >= total_rounds)

    return {
        'round': curr_round,
        'total_rounds': total_rounds,
        'question_in_round': q_in_round,
        'total_in_round': total_in_round,
        'is_last_in_round': is_last_in_round,
        'is_last_round': is_last_round,
    }


def public_question(room):
    idx = room['question_index']
    if idx < 0 or idx >= len(room['questions']):
        return None
    q = room['questions'][idx]
    r_info = get_round_info(room, idx)
    return {
        'index': idx,
        'total': len(room['questions']),
        'question': q['question'],
        'source': q.get('source', ''),
        'deadline': room.get('deadline'),
        'seconds': q.get('seconds', room['settings']['seconds_per_question']),
        'difficulty': q.get('difficulty', 'medium'),
        'time_tier': q.get('time_tier', 'standard'),
        'round': r_info['round'],
        'total_rounds': r_info['total_rounds'],
        'question_in_round': r_info['question_in_round'],
        'total_in_round': r_info['total_in_round'],
    }


def broadcast_lobby(code):
    room = rooms.get(code)
    if not room:
        return
    socketio.emit('lobby_update', {
        'code': code,
        'players': public_players(room),
        'status': room['status'],
        'settings': room['settings'],
        'host_nickname': room['host_nickname']
    }, to=code)


def score_for(correct, elapsed, limit):
    if not correct:
        return 0
    fraction_left = max(0.0, min(1.0, (limit - elapsed) / limit))
    return int(round(500 + 500 * fraction_left))


# ── State Machine & Automatic Progression ─────────────────────────────────────
# State Machine:
# WAITING (lobby) -> starting (3s splash) -> QUESTION_ACTIVE (question)
# -> QUESTION_RESULTS (5s countdown & question leaderboard)
#    -> if more questions in round: QUESTION_ACTIVE
#    -> if last question in round & more rounds: ROUND_RESULTS (10s countdown) -> QUESTION_ACTIVE
#    -> if last question in final round: FINAL_RESULTS (trophy & winner)

def finish_question(code):
    """Lock answers, calculate scores, snapshot ranks, broadcast reveal, auto-schedule next step."""
    with rooms_lock:
        room = rooms.get(code)
        if not room or room['status'] != 'question':
            return

        room['status'] = 'question_results'
        room['deadline'] = None
        idx = room['question_index']
        q = room['questions'][idx]
        r_info = get_round_info(room, idx)

        # Snapshot previous ranks before updating this question's points
        for p in room['players'].values():
            p['previous_rank'] = p.get('current_rank', 1)

        results = []
        for player in room['players'].values():
            submission = player.get('last_submission')
            pts = submission['points_earned'] if submission else 0
            player['last_question_points'] = pts
            player['round_points'] = player.get('round_points', 0) + pts
            results.append({
                'player_id': player['id'],
                'nickname': player['nickname'],
                'answer': submission['answer'] if submission else None,
                'correct': bool(submission and submission['correct']),
                'points_earned': pts,
                'total_score': player['score'],
                'elapsed': round(submission['elapsed'], 2) if submission else None
            })

        results.sort(key=lambda r: (
            -r['points_earned'],
            r['elapsed'] if r['elapsed'] is not None else 9999,
            r['nickname'].lower()
        ))

        countdown_ends_at = time.time() + QUESTION_RESULTS_SECONDS
        room['countdown_ends_at'] = countdown_ends_at

        # Save reveal payload for reconnecting players
        room['last_reveal'] = {
            'question_index': idx,
            'total': len(room['questions']),
            'round': r_info['round'],
            'total_rounds': r_info['total_rounds'],
            'question_in_round': r_info['question_in_round'],
            'total_in_round': r_info['total_in_round'],
            'is_last_in_round': r_info['is_last_in_round'],
            'is_last_round': r_info['is_last_round'],
            'answer': q['answer'],
            'accepted_answers': q.get('accepted_answers', [q['answer']]),
            'explanation': q.get('explanation', ''),
            'source': q.get('source', ''),
            'difficulty': q.get('difficulty', 'medium'),
            'time_tier': q.get('time_tier', 'standard'),
            'seconds': q.get('seconds', room['settings']['seconds_per_question']),
            'results': results,
            'players': public_players(room),
            'countdown_ends_at': countdown_ends_at,
            'countdown_seconds': QUESTION_RESULTS_SECONDS,
        }
        payload = dict(room['last_reveal'])
        question_index_snapshot = idx

    socketio.emit('question_result', payload, to=code)
    socketio.start_background_task(_auto_advance_after_question, code, question_index_snapshot)


def _auto_advance_after_question(code, question_index):
    """Wait QUESTION_RESULTS_SECONDS, then move to next question, round results, or final results."""
    socketio.sleep(QUESTION_RESULTS_SECONDS)

    with rooms_lock:
        room = rooms.get(code)
        if not room or room['status'] != 'question_results' or room['question_index'] != question_index:
            return

        r_info = get_round_info(room, question_index)

        if not r_info['is_last_in_round']:
            # Normal question in round -> Advance to next question
            _advance_to_next_question(room, code)
        elif not r_info['is_last_round']:
            # End of a non-final round -> Transition to ROUND_RESULTS screen (10s countdown)
            _enter_round_results(room, code, r_info)
        else:
            # End of final round -> Transition to FINAL_RESULTS
            _enter_final_results(room, code)


def _enter_round_results(room, code, r_info):
    """Transition to round-end leaderboard screen for ROUND_END_SECONDS."""
    room['status'] = 'round_results'
    room['deadline'] = None
    countdown_ends_at = time.time() + ROUND_END_SECONDS
    room['countdown_ends_at'] = countdown_ends_at

    completed_round = r_info['round']
    players_data = public_players(room)

    room['last_round_results'] = {
        'round': completed_round,
        'next_round': completed_round + 1,
        'total_rounds': r_info['total_rounds'],
        'message': f"Round {completed_round} Complete!",
        'countdown_seconds': ROUND_END_SECONDS,
        'countdown_ends_at': countdown_ends_at,
        'players': players_data,
    }
    payload = dict(room['last_round_results'])

    socketio.emit('round_results', payload, to=code)
    socketio.start_background_task(_auto_advance_after_round, code, completed_round)


def _auto_advance_after_round(code, completed_round):
    """Wait ROUND_END_SECONDS, reset round points, then automatically start the next round."""
    socketio.sleep(ROUND_END_SECONDS)

    with rooms_lock:
        room = rooms.get(code)
        if not room or room['status'] != 'round_results':
            return
        # Reset round points for all players
        for p in room['players'].values():
            p['round_points'] = 0
            p['previous_rank'] = p.get('current_rank', 1)

        _advance_to_next_question(room, code)


def _enter_final_results(room, code):
    """Final results screen at the end of the entire game."""
    room['status'] = 'final_results'
    room['deadline'] = None
    room['countdown_ends_at'] = None
    final_players = public_players(room)
    results_json = json.dumps(final_players)
    with db() as conn:
        conn.execute(
            'INSERT INTO games (room_code, created_at, ended_at, question_count, results_json) VALUES (?, ?, ?, ?, ?)',
            (code, room['created_at'], datetime.now(timezone.utc).isoformat(),
             len(room['questions']), results_json)
        )
    payload = {
        'code': code,
        'players': final_players,
        'question_count': len(room['questions']),
        'winner': final_players[0] if final_players else None,
    }
    room['final_payload'] = payload
    socketio.emit('game_over', payload, to=code)


def _advance_to_next_question(room, code):
    """Increment question index and broadcast question_started."""
    next_index = room['question_index'] + 1
    if next_index >= len(room['questions']):
        _enter_final_results(room, code)
        return

    room['question_index'] = next_index
    room['status'] = 'question'
    room['question_started_at'] = time.time()
    
    current_q = room['questions'][next_index]
    # Tiered / adaptive question timer with fallback to room setting
    q_seconds = current_q.get('seconds') or room['settings'].get('seconds_per_question', DEFAULT_QUESTION_SECONDS)
    room['deadline'] = room['question_started_at'] + q_seconds
    room['countdown_ends_at'] = None
    for p in room['players'].values():
        p['last_submission'] = None
        p['attempt_count'] = 0

    payload = public_question(room)
    deadline_snapshot = room['deadline']
    index_snapshot = next_index

    socketio.emit('question_started', payload, to=code)
    socketio.start_background_task(run_question_timer, code, index_snapshot, deadline_snapshot)


def run_question_timer(code, question_index, deadline):
    """Authoritative timer background task: lock answers and reveal when time expires."""
    delay = max(0, deadline - time.time())
    socketio.sleep(delay)
    with rooms_lock:
        room = rooms.get(code)
        if not room or room['status'] != 'question' or room['question_index'] != question_index:
            return
    finish_question(code)


def start_question(code):
    """Initiate question progression for Question 1."""
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            return
        _advance_to_next_question(room, code)


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get('/api/health')
def health():
    gemini_key = os.getenv('GEMINI_API_KEY')
    return jsonify({
        'ok': True,
        'service': 'Krillearn API',
        'ai_configured': bool(gemini_key),
        'ai_provider': 'gemini' if gemini_key else None,
        'model': os.getenv('GEMINI_MODEL', 'gemini-3.5-flash') if gemini_key else None,
        'timing': {
            'question_seconds': DEFAULT_QUESTION_SECONDS,
            'question_results_seconds': QUESTION_RESULTS_SECONDS,
            'round_end_seconds': ROUND_END_SECONDS,
            'game_start_countdown': GAME_START_COUNTDOWN,
        }
    })


@app.post('/api/questions/generate')
def api_generate_questions():
    if 'file' not in request.files:
        return json_error('Choose a PDF or PPTX file first.')
    uploaded = request.files['file']
    filename = secure_filename(uploaded.filename or '')
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return json_error('Supported formats are PDF and PPTX. Please convert legacy .ppt files to .pptx first.')
    count = request.form.get('question_count', '10')
    difficulty = str(request.form.get('difficulty', 'mixed')).strip().lower()
    if difficulty not in ('mixed', 'easy', 'medium', 'hard', 'numerical'):
        difficulty = 'mixed'
    try:
        count = max(1, min(MAX_QUESTIONS, int(count)))
    except ValueError:
        return json_error('question_count must be a number.')
    if not filename:
        return json_error('The uploaded file needs a filename.')
    stored_path = UPLOAD_DIR / f'{secrets.token_hex(12)}{suffix}'
    uploaded.save(stored_path)
    try:
        pages = extract_document(stored_path)
        if not pages:
            return json_error(
                'No selectable text was found. Scanned PDFs need OCR, which this MVP does not include yet.', 422)
        questions, generation_mode = generate_questions(pages, count, difficulty)
        if not questions:
            return json_error(
                'Could not generate enough questions from this file. '
                'Try a text-rich PDF/PPTX or configure GEMINI_API_KEY.', 422)
        upload_id = secrets.token_urlsafe(32)
        pending_question_sets[upload_id] = {
            'questions': questions,
            'generation_mode': generation_mode,
            'difficulty': difficulty,
            'created_at': time.time()
        }
        for key, item in list(pending_question_sets.items()):
            if time.time() - item['created_at'] > 3600:
                pending_question_sets.pop(key, None)
        return jsonify({
            'upload_id': upload_id,
            'count': len(questions),
            'generation_mode': generation_mode,
            'notice': (
                'Questions are stored privately on the server. The host will not see them before the live round.'
                if generation_mode == 'ai'
                else 'AI is not configured, so heuristic draft questions were generated. '
                     'Configure GEMINI_API_KEY for real AI question generation.'
            )
        })
    except RuntimeError as exc:
        return json_error(str(exc), 422)
    except Exception:
        app.logger.exception('Document processing failed')
        return json_error('Could not process this document. Check that it is a valid PDF or PPTX.', 422)
    finally:
        try:
            stored_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.post('/api/rooms')
def create_room():
    data = request.get_json(silent=True) or {}
    upload_id = str(data.get('upload_id', ''))
    pending = pending_question_sets.pop(upload_id, None) if upload_id else None
    if not pending:
        return json_error('Your private question set expired or was already used. Upload the document again.')
    questions = pending['questions']
    host_nickname = str(data.get('host_nickname', 'Host')).strip()[:24] or 'Host'
    try:
        count = int(data.get('question_count', len(questions or [])))
        seconds = int(data.get('seconds_per_question', DEFAULT_QUESTION_SECONDS))
        q_per_round = int(data.get('questions_per_round', DEFAULT_QUESTIONS_PER_ROUND))
    except (ValueError, TypeError):
        return json_error('Question count, timer, and questions per round must be whole numbers.')
    if not isinstance(questions, list) or not questions:
        return json_error('Generate and review questions before creating a room.')
    if count < 1 or count > len(questions):
        return json_error('Question count must be between 1 and the number of generated questions.')
    if seconds < 5 or seconds > 120:
        return json_error('Each question timer must be between 5 and 120 seconds.')
    if q_per_round < 1 or q_per_round > count:
        q_per_round = count

    safe_questions = []
    for q in questions:
        if not isinstance(q, dict) or not str(q.get('question', '')).strip() or not str(q.get('answer', '')).strip():
            continue
        answer = str(q['answer']).strip()[:100]
        accepted = q.get('accepted_answers') if isinstance(q.get('accepted_answers'), list) else []
        tier = str(q.get('time_tier', 'standard')).strip().lower()
        if tier not in TIER_SECONDS:
            tier = 'standard'
        q_seconds = int(q.get('seconds') or TIER_SECONDS[tier])
        diff = str(q.get('difficulty', 'medium')).strip().lower()
        if diff not in ('easy', 'medium', 'hard', 'numerical'):
            diff = 'medium'

        safe_questions.append({
            'question': str(q['question']).strip()[:500],
            'answer': answer,
            'accepted_answers': list(dict.fromkeys(
                [answer] + [str(x).strip()[:100] for x in accepted if str(x).strip()]
            )),
            'explanation': str(q.get('explanation', '')).strip()[:1000],
            'source': str(q.get('source', '')).strip()[:100],
            'difficulty': diff,
            'time_tier': tier,
            'seconds': q_seconds,
        })

    if not safe_questions:
        return json_error('Each question needs a valid question and answer.')
    if count > len(safe_questions):
        count = len(safe_questions)

    active_questions = safe_questions[:count]
    played_hashes = {hash(q['question'] + '::' + q['answer']) for q in active_questions}

    with rooms_lock:
        code = ''.join(random.SystemRandom().choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
        while code in rooms:
            code = ''.join(random.SystemRandom().choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
        host_token = secrets.token_urlsafe(24)
        total_rounds = max(1, (count + q_per_round - 1) // q_per_round)
        room = {
            'code': code,
            'host_token': host_token,
            'host_nickname': host_nickname,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'questions': active_questions,
            'question_pool': safe_questions,
            'played_hashes': played_hashes,
            'settings': {
                'question_count': count,
                'seconds_per_question': seconds,
                'questions_per_round': q_per_round,
                'total_rounds': total_rounds,
                'question_results_seconds': QUESTION_RESULTS_SECONDS,
                'round_end_seconds': ROUND_END_SECONDS,
            },
            'players': {},
            'status': 'lobby',
            'question_index': -1,
            'deadline': None,
            'question_started_at': None,
            'countdown_ends_at': None,
            'last_reveal': None,
            'last_round_results': None,
            'final_payload': None,
        }
        rooms[code] = room
    return jsonify({'code': code, 'host_token': host_token, 'settings': room['settings']}), 201


# ── Socket.IO Event Handlers ─────────────────────────────────────────────────

@socketio.on('join_game')
def on_join_game(data):
    data = data or {}
    code = str(data.get('code', '')).strip().upper()
    nickname = str(data.get('nickname', '')).strip()[:24]
    if not re.fullmatch(r'[A-Z0-9]{6}', code):
        emit('join_error', {'message': 'Enter a valid 6-character room code.'})
        return
    if not nickname:
        emit('join_error', {'message': 'Choose a nickname first.'})
        return
    with rooms_lock:
        room = rooms.get(code)
        if not room:
            emit('join_error', {'message': 'Room not found. Ask the host for a new code.'})
            return
        if room['status'] not in ('lobby', 'question', 'question_results', 'round_results', 'final_results'):
            emit('join_error', {'message': 'This game has already finished.'})
            return
        existing = next((p for p in room['players'].values() if p['nickname'].lower() == nickname.lower()), None)
        if existing and existing['connected']:
            emit('join_error', {'message': 'That nickname is already in use in this room.'})
            return
        player_id = existing['id'] if existing else secrets.token_urlsafe(10)
        player = existing or {
            'id': player_id, 'nickname': nickname, 'score': 0,
            'connected': True, 'last_submission': None, 'sid': None,
            'last_question_points': 0, 'round_points': 0, 'previous_rank': 1, 'current_rank': 1,
            'rank_change': 0,
        }
        player.update({'nickname': nickname, 'connected': True, 'sid': request.sid})
        room['players'][player_id] = player
        socket_join_room(code)

        state = {
            'code': code, 'player_id': player_id, 'nickname': nickname,
            'status': room['status'], 'host_nickname': room['host_nickname'],
            'settings': room['settings'], 'players': public_players(room),
            'countdown_ends_at': room.get('countdown_ends_at'),
        }
        if room['status'] == 'question':
            state['question'] = public_question(room)
        elif room['status'] == 'question_results' and room.get('last_reveal'):
            state['reveal'] = room['last_reveal']
        elif room['status'] == 'round_results' and room.get('last_round_results'):
            state['round_results'] = room['last_round_results']
        elif room['status'] == 'final_results' and room.get('final_payload'):
            state['final_payload'] = room['final_payload']

    emit('joined_game', state)
    broadcast_lobby(code)


@socketio.on('host_start_game')
def on_host_start(data):
    data = data or {}
    code = str(data.get('code', '')).upper()
    token = str(data.get('host_token', ''))
    with rooms_lock:
        room = rooms.get(code)
        if not room or not secrets.compare_digest(room['host_token'], token):
            emit('action_error', {'message': 'Only the host can start this game.'})
            return
        if room['status'] != 'lobby':
            emit('action_error', {'message': 'The game has already started.'})
            return
        if not room['players']:
            emit('action_error', {'message': 'At least one player must join before starting.'})
            return
        room['status'] = 'starting'

    socketio.emit('game_starting', {'countdown_seconds': GAME_START_COUNTDOWN}, to=code)
    socketio.sleep(GAME_START_COUNTDOWN)
    start_question(code)


@socketio.on('submit_answer')
def on_submit_answer(data):
    data = data or {}
    code = str(data.get('code', '')).upper()
    player_id = str(data.get('player_id', ''))
    answer = str(data.get('answer', ''))[:200]
    with rooms_lock:
        room = rooms.get(code)
        if not room or room['status'] != 'question':
            emit('answer_ack', {'accepted': False, 'message': 'This question is no longer accepting answers.'})
            return
        player = room['players'].get(player_id)
        if not player or player.get('sid') != request.sid:
            emit('answer_ack', {'accepted': False, 'message': 'Your player session is not valid.'})
            return
        # If player already locked in a CORRECT answer, do not accept further answers
        if player.get('last_submission') is not None and player['last_submission'].get('correct'):
            emit('answer_ack', {'accepted': False, 'message': 'You have already locked in the correct answer!'})
            return
        now = time.time()
        if now > room['deadline']:
            emit('answer_ack', {'accepted': False, 'message': 'Time is up.'})
            return

        q = room['questions'][room['question_index']]
        is_correct = is_answer_correct(answer, q['answer'], q.get('accepted_answers'))
        player['attempt_count'] = player.get('attempt_count', 0) + 1
        elapsed = max(0.0, now - room['question_started_at'])
        q_seconds = q.get('seconds') or room['settings']['seconds_per_question']

        if is_correct:
            points = score_for(True, elapsed, q_seconds)
            player['score'] += points
            player['last_question_points'] = points
            player['last_submission'] = {
                'answer': answer.strip(),
                'correct': True,
                'elapsed': elapsed,
                'points_earned': points,
                'attempt_count': player['attempt_count']
            }
            ack = {
                'accepted': True,
                'correct': True,
                'points_earned': points,
                'submitted': True,
                'message': 'Correct answer locked in!'
            }
        else:
            # Multi-attempt: Wrong answer gives inline warning, allows retrying until timer expires
            player['last_submission'] = {
                'answer': answer.strip(),
                'correct': False,
                'elapsed': elapsed,
                'points_earned': 0,
                'attempt_count': player['attempt_count']
            }
            ack = {
                'accepted': True,
                'correct': False,
                'points_earned': 0,
                'submitted': False,
                'message': 'No match — try again!'
            }

        # Check if all connected players have solved the question correctly
        all_solved = all(
            p.get('last_submission') is not None and p['last_submission'].get('correct')
            for p in room['players'].values() if p['connected']
        )

    emit('answer_ack', ack)
    if is_correct:
        socketio.emit('player_answered', {'player_id': player_id, 'nickname': player['nickname']}, to=code, skip_sid=request.sid)
        socketio.emit('leaderboard_update', {'players': public_players(room)}, to=code)
    if all_solved:
        finish_question(code)


@socketio.on('play_again')
def on_play_again(data):
    """Safely reset room for another game with unplayed questions from the pool."""
    data = data or {}
    code = str(data.get('code', '')).upper()
    token = str(data.get('host_token', ''))
    with rooms_lock:
        room = rooms.get(code)
        if not room or not secrets.compare_digest(room['host_token'], token):
            emit('action_error', {'message': 'Only the host can restart this room.'})
            return
        if room['status'] != 'final_results':
            emit('action_error', {'message': 'Cannot restart until the game is completed.'})
            return

        pool = room.get('question_pool', room.get('questions', []))
        played_hashes = room.get('played_hashes', set())
        needed_count = room['settings'].get('question_count', len(room['questions']))

        # Find unplayed questions
        unplayed = [
            q for q in pool
            if hash(q['question'] + '::' + q['answer']) not in played_hashes
        ]

        if len(unplayed) >= needed_count:
            next_questions = unplayed[:needed_count]
        elif len(unplayed) > 0:
            # Use remaining unplayed + wrap around rest of pool shuffled
            remaining = [
                q for q in pool
                if hash(q['question'] + '::' + q['answer']) in played_hashes
            ]
            random.shuffle(remaining)
            next_questions = (unplayed + remaining)[:needed_count]
            # Reset played set to these questions
            played_hashes.clear()
        else:
            # Pool exhausted: shuffle full pool so questions appear in fresh order
            shuffled = list(pool)
            random.shuffle(shuffled)
            next_questions = shuffled[:needed_count]
            played_hashes.clear()

        # Update played hashes
        for q in next_questions:
            played_hashes.add(hash(q['question'] + '::' + q['answer']))

        room['played_hashes'] = played_hashes
        room['questions'] = next_questions
        room['status'] = 'lobby'
        room['question_index'] = -1
        room['deadline'] = None
        room['question_started_at'] = None
        room['countdown_ends_at'] = None
        room['last_reveal'] = None
        room['last_round_results'] = None
        room['final_payload'] = None
        for p in room['players'].values():
            p['score'] = 0
            p['last_submission'] = None
            p['last_question_points'] = 0
            p['round_points'] = 0
            p['attempt_count'] = 0
            p['previous_rank'] = 1
            p['current_rank'] = 1
            p['rank_change'] = 0

    socketio.emit('room_reset', {
        'code': code,
        'status': 'lobby',
        'players': public_players(room),
        'settings': room['settings'],
    }, to=code)


@socketio.on('disconnect')
def on_disconnect(reason=None):
    with rooms_lock:
        for code, room in rooms.items():
            for player in room['players'].values():
                if player.get('sid') == request.sid:
                    player['connected'] = False
                    player['sid'] = None
                    socketio.start_background_task(broadcast_lobby, code)
                    return


@app.errorhandler(413)
def too_large(_):
    return json_error('File is too large. The upload limit is 20 MB.', 413)

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '5000')),
                 debug=os.getenv('FLASK_DEBUG') == '1', allow_unsafe_werkzeug=True)
