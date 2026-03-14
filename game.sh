#!/bin/bash
# KradleVerse game loop script
set -e

API_KEY=$(grep KRADLEVERSE_API_KEY ~/.kradle/kradleverse/.env | cut -d= -f2-)
BASE="https://kradleverse.com/api/v1"

act() {
  local RUN_ID="$1"
  local CODE="$2"
  local MSG="$3"
  local THOUGHTS="$4"

  # Build JSON payload using python to handle escaping
  python3 -c "
import json, sys
payload = {}
if sys.argv[1]: payload['code'] = sys.argv[1]
if sys.argv[2]: payload['message'] = sys.argv[2]
if sys.argv[3]: payload['thoughts'] = sys.argv[3]
print(json.dumps(payload))
" "$CODE" "$MSG" "$THOUGHTS" | curl -s -X POST "$BASE/runs/$RUN_ID/actions" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d @-
}

observe() {
  local RUN_ID="$1"
  local CURSOR="$2"
  local URL="$BASE/runs/$RUN_ID/observations"
  if [ -n "$CURSOR" ]; then
    URL="$URL?cursor=$CURSOR"
  fi
  curl -s -H "Authorization: Bearer $API_KEY" "$URL"
}

parse_obs() {
  python3 -c "
import sys, json
raw = sys.stdin.read()
data = json.loads(raw, strict=False)
state = data.get('stateAtLastObservation', {})
cursor = data.get('nextPageToken', '')

game_over = False
has_init = False
task = ''
biome = state.get('biome', '')
executing = state.get('executing', False)
health = state.get('health', 0)
position = state.get('position', {})
inventory = state.get('inventory', {})
score = state.get('score', 0)
winner = state.get('winner', False)

events = []
chat_msgs = []
output = ''
next_steps = []

for obs in data.get('observations', []):
    d = obs.get('data', {})
    event = d.get('event', 'init_call')
    events.append(event)
    if 'task' in d:
        has_init = True
        task = d['task'][:500]
    if event == 'game_over':
        game_over = True
        next_steps = d.get('nextSteps', [])
    if d.get('chatMessages'):
        for cm in d['chatMessages']:
            chat_msgs.append(f\"{cm.get('sender','?')}: {cm.get('message','')}\")
    if d.get('output'):
        output = str(d['output'])[-500:]
    if event == 'command_executed' and d.get('output'):
        output = str(d['output'])[-500:]

print(f'CURSOR={cursor}')
print(f'GAME_OVER={game_over}')
print(f'HAS_INIT={has_init}')
print(f'EXECUTING={executing}')
print(f'HEALTH={health}')
print(f'BIOME={biome}')
print(f'SCORE={score}')
print(f'WINNER={winner}')
print(f'EVENTS={\"|\".join(events)}')
print(f'POSITION={json.dumps(position)}')
print(f'INVENTORY={json.dumps(inventory)}')
if task:
    print(f'TASK={task}')
if output:
    print(f'OUTPUT={output}')
if chat_msgs:
    print(f'CHAT={\"|\".join(chat_msgs[-5:])}')
if next_steps:
    print(f'NEXTSTEPS={json.dumps(next_steps)}')
"
}

echo "Script ready. Pass RUN_ID as argument."
