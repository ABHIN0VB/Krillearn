import time
from app import app, socketio, pending_question_sets

def run_tests():
    client = app.test_client()

    # 4 questions total, 2 questions per round => 2 rounds
    dummy_questions = [
        {
            'question': f'What is test question {i}?',
            'answer': f'Answer{i}',
            'accepted_answers': [f'Answer{i}', f'answer{i}'],
            'explanation': f'Explanation {i}',
            'source': f'Slide {i}'
        } for i in range(1, 5)
    ]
    upload_id = 'test_upload_lifecycle'
    pending_question_sets[upload_id] = {
        'questions': dummy_questions,
        'generation_mode': 'fallback',
        'created_at': time.time()
    }

    # Create room
    res = client.post('/api/rooms', json={
        'upload_id': upload_id,
        'question_count': 4,
        'seconds_per_question': 5,
        'questions_per_round': 2,
        'host_nickname': 'HostUser'
    })
    assert res.status_code == 201
    room_data = res.get_json()
    code = room_data['code']
    host_token = room_data['host_token']
    assert room_data['settings']['total_rounds'] == 2

    sio_host = socketio.test_client(app)
    sio_player = socketio.test_client(app)

    sio_host.emit('join_game', {'code': code, 'nickname': 'HostUser'})
    host_events = sio_host.get_received()
    host_id = next(e for e in host_events if e['name'] == 'joined_game')['args'][0]['player_id']

    sio_player.emit('join_game', {'code': code, 'nickname': 'PlayerUser'})
    player_events = sio_player.get_received()
    player_id = next(e for e in player_events if e['name'] == 'joined_game')['args'][0]['player_id']

    # 1. Start game
    sio_host.emit('host_start_game', {'code': code, 'host_token': host_token})
    time.sleep(3.2) # wait for 3s splash

    # Q1 started
    received = sio_player.get_received()
    q1 = next(e for e in received if e['name'] == 'question_started')['args'][0]
    assert q1['index'] == 0
    assert q1['round'] == 1
    assert q1['question_in_round'] == 1
    print('Round 1 - Question 1 started.')

    # Answer Q1
    sio_player.emit('submit_answer', {'code': code, 'player_id': player_id, 'answer': 'Answer1'})
    sio_host.emit('submit_answer', {'code': code, 'player_id': host_id, 'answer': 'Answer1'})
    time.sleep(0.3)

    # Q1 result emitted
    received = sio_player.get_received()
    assert any(e['name'] == 'question_result' for e in received)
    print('Q1 results displayed. Waiting 5s for auto-advance to Q2...')

    # Wait 5.3s for Q2
    time.sleep(5.3)
    received = sio_player.get_received()
    q2 = next(e for e in received if e['name'] == 'question_started')['args'][0]
    assert q2['index'] == 1
    assert q2['round'] == 1
    assert q2['question_in_round'] == 2
    print('Round 1 - Question 2 started automatically!')

    # Answer Q2 (Last question of Round 1)
    sio_player.emit('submit_answer', {'code': code, 'player_id': player_id, 'answer': 'Answer2'})
    sio_host.emit('submit_answer', {'code': code, 'player_id': host_id, 'answer': 'Answer2'})
    time.sleep(0.3)

    # Q2 results emitted (5s reveal)
    received = sio_player.get_received()
    assert any(e['name'] == 'question_result' for e in received)
    print('Q2 results displayed. Waiting 5s for transition to ROUND_RESULTS...')

    # Wait 5.3s -> should enter round_results (10s countdown)
    time.sleep(5.3)
    received = sio_player.get_received()
    round_res = next((e for e in received if e['name'] == 'round_results'), None)
    assert round_res is not None, 'round_results event was not emitted!'
    payload = round_res['args'][0]
    print(f"Round {payload['round']} Complete! Message: {payload['message']}")
    print(f"Countdown seconds: {payload['countdown_seconds']}")
    assert payload['countdown_seconds'] == 10

    # Wait 10.3s for Round 2 to start automatically
    print('Waiting 10s for Round 2 to start automatically...')
    time.sleep(10.3)
    received = sio_player.get_received()
    q3 = next((e for e in received if e['name'] == 'question_started'), None)
    assert q3 is not None, 'Round 2 Question 1 did not start automatically!'
    assert q3['args'][0]['round'] == 2
    assert q3['args'][0]['question_in_round'] == 1
    print('Round 2 started automatically! Question:', q3['args'][0]['question'])

    # Answer Q3
    sio_player.emit('submit_answer', {'code': code, 'player_id': player_id, 'answer': 'Answer3'})
    sio_host.emit('submit_answer', {'code': code, 'player_id': host_id, 'answer': 'Answer3'})
    time.sleep(0.3)
    time.sleep(5.3) # 5s reveal -> Q4

    received = sio_player.get_received()
    q4 = next((e for e in received if e['name'] == 'question_started'), None)
    assert q4 is not None
    print('Round 2 - Question 4 started.')

    # Answer Q4 (Final question of final round!)
    sio_player.emit('submit_answer', {'code': code, 'player_id': player_id, 'answer': 'Answer4'})
    sio_host.emit('submit_answer', {'code': code, 'player_id': host_id, 'answer': 'Answer4'})
    time.sleep(0.3)
    print('Q4 answered. Waiting 5s for transition to FINAL_RESULTS...')

    time.sleep(5.3)
    received = sio_player.get_received()
    game_over = next((e for e in received if e['name'] == 'game_over'), None)
    assert game_over is not None, 'Final game_over event was not emitted!'
    final_payload = game_over['args'][0]
    print('Tournament Complete! Winner:', final_payload['winner']['nickname'], 'with', final_payload['winner']['score'], 'points!')

    # Test Play Again
    sio_host.emit('play_again', {'code': code, 'host_token': host_token})
    time.sleep(0.5)
    received = sio_player.get_received()
    reset_ev = next((e for e in received if e['name'] == 'room_reset'), None)
    assert reset_ev is not None, 'room_reset event not emitted on play_again!'
    assert reset_ev['args'][0]['status'] == 'lobby'
    print('Play Again successfully reset room to lobby!')

    print('\n======================================================')
    print('FULL GAME LIFECYCLE & ROUND PROGRESSION VERIFIED 100%!')
    print('======================================================')

if __name__ == '__main__':
    run_tests()
