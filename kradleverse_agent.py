#!/usr/bin/env python3
"""KradleVerse autonomous game agent for MaximumOpus."""

import json
import os
import sys
import time
import requests

# --- Config ---
ENV_PATH = os.path.expanduser("~/.kradle/kradleverse/.env")
BASE_URL = "https://kradleverse.com/api/v1"

def load_credentials():
    creds = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    return creds

CREDS = load_credentials()
API_KEY = CREDS["KRADLEVERSE_API_KEY"]
AGENT_NAME = CREDS["KRADLEVERSE_AGENT_NAME"]
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def api_get(path, params=None, retries=3):
    url = f"{BASE_URL}/{path}" if not path.startswith("http") else path
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=15)
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == retries - 1:
                print(f"[API] GET failed after {retries} retries: {e}")
                return {"success": False, "error": str(e)}
            time.sleep(2)


def api_post(path, data=None, retries=3):
    url = f"{BASE_URL}/{path}" if not path.startswith("http") else path
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=HEADERS, json=data or {}, timeout=15)
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == retries - 1:
                print(f"[API] POST failed after {retries} retries: {e}")
                return {"success": False, "error": str(e)}
            time.sleep(2)


# ── Queue & Connect ──────────────────────────────────────────────

def leave_queue():
    """Leave any existing queue entries."""
    try:
        r = requests.delete(f"{BASE_URL}/queue", headers=HEADERS, timeout=10)
        return r.json()
    except Exception:
        return {}


def join_queue(**kwargs):
    print("[QUEUE] Joining matchmaking...")
    resp = api_post("queue", kwargs or {})
    if not resp.get("success") and resp.get("code") == "RATE_LIMITED":
        print("[QUEUE] Already in queue, leaving first...")
        leave_queue()
        time.sleep(1)
        resp = api_post("queue", kwargs or {})
    print(f"[QUEUE] {resp.get('message', resp)}")
    return resp


def wait_for_connection(timeout=180):
    """Poll queue until status == 'connected'. Returns run_id."""
    deadline = time.time() + timeout
    run_id = None
    printed_link = False
    while time.time() < deadline:
        resp = api_get("queue")
        entries = resp.get("entries", [])
        if not entries:
            time.sleep(3)
            continue
        entry = entries[0]
        status = entry["status"]
        run_obj = entry.get("run") or {}
        rid = run_obj.get("runId", "")

        if status == "matched" and rid and not printed_link:
            print(f"\n{'='*60}")
            print(f"  MATCHED! Watch live:")
            print(f"  https://kradleverse.com/watch/{rid}")
            print(f"{'='*60}\n")
            printed_link = True

        if status == "connected" and rid:
            run_id = rid
            print(f"[QUEUE] Connected! runId={run_id}")
            return run_id

        time.sleep(2)
    raise TimeoutError("Failed to connect within timeout")


# ── Observe & Act ────────────────────────────────────────────────

def observe(run_id, cursor=None):
    params = {"cursor": cursor} if cursor else {}
    return api_get(f"runs/{run_id}/observations", params)


def act(run_id, code=None, message=None, thoughts=None):
    payload = {}
    if code:
        payload["code"] = code
    if message:
        payload["message"] = message
    if thoughts:
        payload["thoughts"] = thoughts
    if not payload.get("code") and not payload.get("message"):
        return {"success": False, "error": "empty action"}
    resp = api_post(f"runs/{run_id}/actions", payload)
    if resp.get("success"):
        print(f"[ACT] Action sent (id={resp.get('actionId','?')[:8]})")
    else:
        print(f"[ACT] FAILED: {resp.get('error','?')}")
    return resp


# ── Game State Tracker ───────────────────────────────────────────

class GameState:
    def __init__(self):
        self.cursor = None
        self.task = ""
        self.biome = ""
        self.position = {}
        self.inventory = {}
        self.health = 0
        self.score = 0
        self.winner = False
        self.executing = False
        self.game_over = False
        self.players = {}
        self.entities = []
        self.blocks = []
        self.craftable = []
        self.chat_log = []
        self.last_output = ""
        self.next_steps = []
        self.events = []
        self.has_init = False
        self.challenge_type = ""  # "zombie", "building", "unknown"
        self.js_functions = {}
        self.surrounding_blocks = []
        self.elapsed_ms = 0
        self.lives = 1
        self.armor = {}
        self.held_item = ""
        self.weather = ""
        self.time_of_day = ""
        self.contact_blocks = {}
        self.action_count = 0
        self.voted = False
        self.detected_biome = ""
        self.is_skywars = False

    def update(self, obs_response):
        """Process an observations response and update state."""
        state = obs_response.get("stateAtLastObservation") or {}
        self.cursor = obs_response.get("nextPageToken", self.cursor)

        # Update from aggregated state
        for key in ["biome", "health", "score", "winner", "executing",
                     "position", "inventory", "players", "entities",
                     "blocks", "craftable", "armor", "heldItem",
                     "weather", "timeOfDay", "contactBlocks", "lives"]:
            if key in state:
                setattr(self, key.lower() if key == key.lower() else
                        # camelCase → snake_case for specific fields
                        {"heldItem": "held_item", "timeOfDay": "time_of_day",
                         "contactBlocks": "contact_blocks"}.get(key, key),
                        state[key])

        self.events = []
        for obs in obs_response.get("observations", []):
            d = obs.get("data", {})
            event = d.get("event", "init_call")
            self.events.append(event)
            self.elapsed_ms = d.get("elapsedMs", self.elapsed_ms)

            if "task" in d:
                self.has_init = True
                self.task = d["task"]
                self.js_functions = d.get("js_functions", {})
                self._detect_challenge_type()

            if event == "initial_state":
                self.surrounding_blocks = d.get("surroundingBlocksRelative", [])
                # Pull inline state from initial_state
                for k in ["position", "inventory", "health", "lives",
                           "biome", "blocks", "entities", "craftable"]:
                    if k in d:
                        setattr(self, k, d[k])

            if event == "game_over":
                self.game_over = True
                self.next_steps = d.get("nextSteps", [])
                if "winner" in d:
                    self.winner = d["winner"]
                if "score" in d:
                    self.score = d["score"]

            if event in ("command_executed", "command_progress"):
                self.last_output = str(d.get("output", ""))[-600:]

            if d.get("chatMessages"):
                for cm in d["chatMessages"]:
                    self.chat_log.append(cm)

    def _detect_challenge_type(self):
        task_lower = self.task.lower()
        if "zombie" in task_lower and "survive" in task_lower:
            self.challenge_type = "zombie"
        elif "battle royale" in task_lower or "last one standing" in task_lower:
            self.challenge_type = "battle_royale"
        elif "build" in task_lower and ("biome" in task_lower or "creative" in task_lower or "voting" in task_lower):
            self.challenge_type = "building"
        elif "harvest" in task_lower or "farm" in task_lower or "crop" in task_lower:
            self.challenge_type = "harvest"
        elif "survive" in task_lower:
            self.challenge_type = "survival"
        elif "collect" in task_lower or "gather" in task_lower:
            self.challenge_type = "collect"
        elif "pig" in task_lower:
            self.challenge_type = "pig_farming"
        else:
            self.challenge_type = "adaptive"

    def recent_chat(self, n=5):
        return self.chat_log[-n:]

    def other_players(self):
        return [p for p in self.players.keys()]


# ── Strategy: Zombie Apocalypse ──────────────────────────────────

ZOMBIE_PHASE1 = """
// PHASE 1: Enable survival modes and build shelter FAST
await skills.setMode(bot, 'self_defense', true);
await skills.setMode(bot, 'self_preservation', true);
await skills.setMode(bot, 'item_collecting', true);

let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Build a quick 3x3 cobblestone box (walls + roof) around me
// Walls - 3 high
for (let y = 0; y <= 2; y++) {
  for (let x = -1; x <= 1; x++) {
    for (let z = -1; z <= 1; z++) {
      if (y === 2 || Math.abs(x) === 1 || Math.abs(z) === 1) {
        // Leave entrance at z=-1, x=0, y=0 (will seal later)
        if (!(y === 0 && x === 0 && z === -1)) {
          await skills.placeBlock(bot, 'cobblestone', px + x, py + y, pz + z);
        }
      }
    }
  }
}
// Place torch inside
await skills.placeBlock(bot, 'torch', px, py + 1, pz);
skills.log(bot, 'Shelter built! Sealing entrance...');

// Seal the entrance
await skills.placeBlock(bot, 'cobblestone', px, py, pz - 1);
await skills.placeBlock(bot, 'cobblestone', px, py + 1, pz - 1);
skills.log(bot, 'Sealed in! Now I wait safely.');
"""

ZOMBIE_PHASE2_DEFEND = """
// Equip sword and defend from inside shelter
await skills.equip(bot, 'iron_sword');
// Continuously defend - attack anything that gets close
let defended = await skills.defendSelf(bot, 5);
if (!defended) {
  // Nothing nearby, wait a bit
  await skills.wait(bot, 3);
}
skills.log(bot, 'Health: ' + bot.health + ' | Defending...');
"""

ZOMBIE_WAIT_SAFE = """
// Stay safe inside shelter, only fight if needed
await skills.equip(bot, 'iron_sword');
let entities = world.getNearbyEntityTypes(bot);
skills.log(bot, 'Nearby entities: ' + JSON.stringify(entities));
let zombie = world.getNearestEntityWhere(bot, e => e.name === 'zombie', 5);
if (zombie) {
  await skills.attackEntity(bot, zombie, true);
  skills.log(bot, 'Killed a zombie!');
} else {
  await skills.wait(bot, 2);
  skills.log(bot, 'Safe for now. Health: ' + bot.health);
}
"""


# ── Strategy: Building Challenge ─────────────────────────────────

def get_building_code_phase1(biome):
    """Return code to build a creative structure for the given biome."""
    builds = {
        "nether": """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Nether Portal Gateway - iconic and impressive
// Base platform with magma
for (let x = -3; x <= 3; x++) {
  for (let z = -3; z <= 3; z++) {
    if (Math.abs(x) <= 1 && Math.abs(z) <= 1) {
      await skills.placeBlock(bot, 'magma_block', px+x, py-1, pz+z);
    } else {
      await skills.placeBlock(bot, 'nether_bricks', px+x, py-1, pz+z);
    }
  }
}

// Giant nether portal frame (5 wide, 7 tall)
for (let y = 0; y <= 6; y++) {
  await skills.placeBlock(bot, 'obsidian', px-2, py+y, pz);
  await skills.placeBlock(bot, 'obsidian', px+2, py+y, pz);
}
for (let x = -2; x <= 2; x++) {
  await skills.placeBlock(bot, 'obsidian', px+x, py+6, pz);
  await skills.placeBlock(bot, 'obsidian', px+x, py, pz);
}
// Fill portal interior with purple glass for portal effect
for (let y = 1; y <= 5; y++) {
  for (let x = -1; x <= 1; x++) {
    await skills.placeBlock(bot, 'purple_stained_glass', px+x, py+y, pz);
  }
}

// Nether brick pillars flanking the portal
for (let y = 0; y <= 4; y++) {
  await skills.placeBlock(bot, 'nether_bricks', px-3, py+y, pz);
  await skills.placeBlock(bot, 'nether_bricks', px+3, py+y, pz);
}
// Soul lanterns on pillars
await skills.placeBlock(bot, 'soul_lantern', px-3, py+5, pz);
await skills.placeBlock(bot, 'soul_lantern', px+3, py+5, pz);

// Lava cascades on sides
await skills.placeBlock(bot, 'lava', px-3, py+4, pz-1);
await skills.placeBlock(bot, 'lava', px+3, py+4, pz+1);

// Crimson plants for atmosphere
await skills.placeBlock(bot, 'crimson_fungus', px-1, py, pz+2);
await skills.placeBlock(bot, 'crimson_fungus', px+1, py, pz+2);
await skills.placeBlock(bot, 'warped_fungus', px-1, py, pz-2);
await skills.placeBlock(bot, 'warped_fungus', px+1, py, pz-2);

skills.log(bot, 'Giant Nether Portal Gateway complete!');
""",
        "plains": """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Cozy cottage with garden
// Foundation
for (let x = -3; x <= 3; x++) {
  for (let z = -3; z <= 3; z++) {
    await skills.placeBlock(bot, 'oak_planks', px+x, py-1, pz+z);
  }
}
// Walls (3 high, oak logs corners, oak planks walls)
for (let y = 0; y <= 2; y++) {
  for (let x = -3; x <= 3; x++) {
    for (let z = -3; z <= 3; z++) {
      if (Math.abs(x) === 3 || Math.abs(z) === 3) {
        if (Math.abs(x) === 3 && Math.abs(z) === 3) {
          await skills.placeBlock(bot, 'oak_log', px+x, py+y, pz+z);
        } else if (y === 1 && (x === 0 || z === 0)) {
          await skills.placeBlock(bot, 'glass_pane', px+x, py+y, pz+z);
        } else {
          await skills.placeBlock(bot, 'oak_planks', px+x, py+y, pz+z);
        }
      }
    }
  }
}
// Roof - peaked
for (let z = -4; z <= 4; z++) {
  for (let x = -3; x <= 3; x++) {
    await skills.placeBlock(bot, 'oak_stairs', px+x, py+3, pz+z);
  }
}
for (let z = -3; z <= 3; z++) {
  for (let x = -2; x <= 2; x++) {
    await skills.placeBlock(bot, 'oak_planks', px+x, py+4, pz+z);
  }
}
// Chimney
await skills.placeBlock(bot, 'bricks', px+2, py+4, pz+2);
await skills.placeBlock(bot, 'bricks', px+2, py+5, pz+2);
await skills.placeBlock(bot, 'campfire', px+2, py+6, pz+2);

// Flower garden
await skills.placeBlock(bot, 'rose_bush', px-4, py, pz);
await skills.placeBlock(bot, 'sunflower', px-4, py, pz+2);
await skills.placeBlock(bot, 'peony', px-4, py, pz-2);
// Lanterns
await skills.placeBlock(bot, 'lantern', px, py, pz-4);
await skills.placeBlock(bot, 'lantern', px, py, pz+4);

skills.log(bot, 'Cozy Plains Cottage with garden complete!');
""",
        "desert": """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Egyptian pyramid with entrance
// Base layer 9x9
for (let layer = 0; layer < 5; layer++) {
  let size = 4 - layer;
  for (let x = -size; x <= size; x++) {
    for (let z = -size; z <= size; z++) {
      await skills.placeBlock(bot, 'sandstone', px+x, py+layer, pz+z);
    }
  }
}
// Gold cap
await skills.placeBlock(bot, 'gold_block', px, py+5, pz);

// Entrance pillars
for (let y = 0; y <= 3; y++) {
  await skills.placeBlock(bot, 'chiseled_sandstone', px-1, py+y, pz-5);
  await skills.placeBlock(bot, 'chiseled_sandstone', px+1, py+y, pz-5);
}
// Torches at entrance
await skills.placeBlock(bot, 'soul_lantern', px-1, py+4, pz-5);
await skills.placeBlock(bot, 'soul_lantern', px+1, py+4, pz-5);

// Oasis pool
for (let x = 3; x <= 5; x++) {
  for (let z = -1; z <= 1; z++) {
    await skills.placeBlock(bot, 'water', px+x, py-1, pz+z);
  }
}
// Palm tree
for (let y = 0; y <= 4; y++) {
  await skills.placeBlock(bot, 'jungle_log', px+4, py+y, pz+2);
}
await skills.placeBlock(bot, 'jungle_leaves', px+3, py+5, pz+2);
await skills.placeBlock(bot, 'jungle_leaves', px+5, py+5, pz+2);
await skills.placeBlock(bot, 'jungle_leaves', px+4, py+5, pz+1);
await skills.placeBlock(bot, 'jungle_leaves', px+4, py+5, pz+3);
await skills.placeBlock(bot, 'jungle_leaves', px+4, py+6, pz+2);

skills.log(bot, 'Desert Pyramid with oasis complete!');
""",
        "ice": """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Crystal Ice Palace
// Base - packed ice platform
for (let x = -3; x <= 3; x++) {
  for (let z = -3; z <= 3; z++) {
    await skills.placeBlock(bot, 'packed_ice', px+x, py-1, pz+z);
  }
}
// Walls of blue ice with glass windows
for (let y = 0; y <= 3; y++) {
  for (let x = -3; x <= 3; x++) {
    for (let z = -3; z <= 3; z++) {
      if (Math.abs(x) === 3 || Math.abs(z) === 3) {
        if (y === 1 && (x === 0 || z === 0)) {
          await skills.placeBlock(bot, 'light_blue_stained_glass', px+x, py+y, pz+z);
        } else {
          await skills.placeBlock(bot, 'blue_ice', px+x, py+y, pz+z);
        }
      }
    }
  }
}
// Spire
for (let y = 4; y <= 8; y++) {
  let r = Math.max(0, 3 - (y - 3));
  for (let x = -r; x <= r; x++) {
    for (let z = -r; z <= r; z++) {
      if (Math.abs(x) === r || Math.abs(z) === r) {
        await skills.placeBlock(bot, 'blue_ice', px+x, py+y, pz+z);
      }
    }
  }
}
await skills.placeBlock(bot, 'sea_lantern', px, py+9, pz);

// Ice pillars outside
for (let y = 0; y <= 5; y++) {
  await skills.placeBlock(bot, 'packed_ice', px-4, py+y, pz-4);
  await skills.placeBlock(bot, 'packed_ice', px+4, py+y, pz-4);
  await skills.placeBlock(bot, 'packed_ice', px-4, py+y, pz+4);
  await skills.placeBlock(bot, 'packed_ice', px+4, py+y, pz+4);
}

skills.log(bot, 'Crystal Ice Palace complete!');
""",
        "jungle": """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Giant Treehouse
// Tree trunk
for (let y = 0; y <= 7; y++) {
  await skills.placeBlock(bot, 'jungle_log', px, py+y, pz);
  if (y < 3) {
    await skills.placeBlock(bot, 'jungle_log', px+1, py+y, pz);
    await skills.placeBlock(bot, 'jungle_log', px, py+y, pz+1);
    await skills.placeBlock(bot, 'jungle_log', px+1, py+y, pz+1);
  }
}
// Platform at height 4
for (let x = -3; x <= 3; x++) {
  for (let z = -3; z <= 3; z++) {
    await skills.placeBlock(bot, 'jungle_planks', px+x, py+4, pz+z);
  }
}
// Railing
for (let x = -3; x <= 3; x++) {
  await skills.placeBlock(bot, 'jungle_fence', px+x, py+5, pz-3);
  await skills.placeBlock(bot, 'jungle_fence', px+x, py+5, pz+3);
}
for (let z = -3; z <= 3; z++) {
  await skills.placeBlock(bot, 'jungle_fence', px-3, py+5, pz+z);
  await skills.placeBlock(bot, 'jungle_fence', px+3, py+5, pz+z);
}
// Leaf canopy
for (let x = -4; x <= 4; x++) {
  for (let z = -4; z <= 4; z++) {
    if (Math.abs(x) + Math.abs(z) <= 5) {
      await skills.placeBlock(bot, 'jungle_leaves', px+x, py+8, pz+z);
    }
  }
}
// Vines
await skills.placeBlock(bot, 'vine', px-3, py+7, pz);
await skills.placeBlock(bot, 'vine', px+3, py+7, pz);
// Lanterns
await skills.placeBlock(bot, 'lantern', px-2, py+5, pz-2);
await skills.placeBlock(bot, 'lantern', px+2, py+5, pz+2);
// Cocoa beans decorations
await skills.placeBlock(bot, 'melon', px-4, py, pz);

skills.log(bot, 'Jungle Treehouse complete!');
""",
        "end": """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Void Observatory with End crystals
// Obsidian base
for (let x = -3; x <= 3; x++) {
  for (let z = -3; z <= 3; z++) {
    await skills.placeBlock(bot, 'obsidian', px+x, py-1, pz+z);
  }
}
// Purpur pillar tower
for (let y = 0; y <= 7; y++) {
  await skills.placeBlock(bot, 'purpur_pillar', px-2, py+y, pz-2);
  await skills.placeBlock(bot, 'purpur_pillar', px+2, py+y, pz-2);
  await skills.placeBlock(bot, 'purpur_pillar', px-2, py+y, pz+2);
  await skills.placeBlock(bot, 'purpur_pillar', px+2, py+y, pz+2);
}
// End stone brick walls
for (let y = 0; y <= 5; y++) {
  for (let x = -2; x <= 2; x++) {
    await skills.placeBlock(bot, 'end_stone_bricks', px+x, py+y, pz-2);
    await skills.placeBlock(bot, 'end_stone_bricks', px+x, py+y, pz+2);
  }
  for (let z = -1; z <= 1; z++) {
    await skills.placeBlock(bot, 'end_stone_bricks', px-2, py+y, pz+z);
    await skills.placeBlock(bot, 'end_stone_bricks', px+2, py+y, pz+z);
  }
}
// Floating end rod beacon
for (let y = 6; y <= 10; y++) {
  await skills.placeBlock(bot, 'end_rod', px, py+y, pz);
}
// Purple glass dome
for (let x = -2; x <= 2; x++) {
  for (let z = -2; z <= 2; z++) {
    await skills.placeBlock(bot, 'purple_stained_glass', px+x, py+6, pz+z);
  }
}
await skills.placeBlock(bot, 'dragon_head', px, py+11, pz);

skills.log(bot, 'End Void Observatory complete!');
""",
        "ocean": """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Lighthouse
// Stone base
for (let x = -2; x <= 2; x++) {
  for (let z = -2; z <= 2; z++) {
    await skills.placeBlock(bot, 'stone_bricks', px+x, py-1, pz+z);
  }
}
// Tower - alternating white and red
for (let y = 0; y <= 8; y++) {
  let block = y % 2 === 0 ? 'white_concrete' : 'red_concrete';
  for (let x = -1; x <= 1; x++) {
    for (let z = -1; z <= 1; z++) {
      if (Math.abs(x) === 1 || Math.abs(z) === 1) {
        await skills.placeBlock(bot, block, px+x, py+y, pz+z);
      }
    }
  }
}
// Light room - glass
for (let x = -1; x <= 1; x++) {
  for (let z = -1; z <= 1; z++) {
    await skills.placeBlock(bot, 'glass', px+x, py+9, pz+z);
  }
}
await skills.placeBlock(bot, 'glowstone', px, py+9, pz);
// Roof
for (let x = -2; x <= 2; x++) {
  for (let z = -2; z <= 2; z++) {
    await skills.placeBlock(bot, 'dark_oak_slab', px+x, py+10, pz+z);
  }
}
await skills.placeBlock(bot, 'sea_lantern', px, py+11, pz);

// Dock
for (let z = -5; z <= -3; z++) {
  for (let x = -1; x <= 1; x++) {
    await skills.placeBlock(bot, 'oak_planks', px+x, py-1, pz+z);
  }
}
// Water around
for (let x = 3; x <= 5; x++) {
  for (let z = -1; z <= 1; z++) {
    await skills.placeBlock(bot, 'water', px+x, py-1, pz+z);
  }
}

skills.log(bot, 'Lighthouse with dock complete!');
""",
        "badlands": """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Western Saloon
// Foundation
for (let x = -3; x <= 3; x++) {
  for (let z = -3; z <= 3; z++) {
    await skills.placeBlock(bot, 'red_sandstone', px+x, py-1, pz+z);
  }
}
// Walls
for (let y = 0; y <= 3; y++) {
  for (let x = -3; x <= 3; x++) {
    for (let z = -3; z <= 3; z++) {
      if (Math.abs(x) === 3 || Math.abs(z) === 3) {
        if (y === 1 && x === 0 && z === -3) {
          // Door opening
        } else if (y === 1 && (x === 0 || z === 0) && (Math.abs(x) === 3 || Math.abs(z) === 3)) {
          await skills.placeBlock(bot, 'glass_pane', px+x, py+y, pz+z);
        } else {
          await skills.placeBlock(bot, 'orange_terracotta', px+x, py+y, pz+z);
        }
      }
    }
  }
}
// Saloon sign (raised front wall)
for (let x = -3; x <= 3; x++) {
  await skills.placeBlock(bot, 'dark_oak_planks', px+x, py+4, pz-3);
}
// Roof
for (let x = -4; x <= 4; x++) {
  for (let z = -4; z <= 4; z++) {
    await skills.placeBlock(bot, 'acacia_slab', px+x, py+4, pz+z);
  }
}
// Porch
for (let x = -3; x <= 3; x++) {
  await skills.placeBlock(bot, 'acacia_fence', px+x, py, pz-4);
}
// Lanterns
await skills.placeBlock(bot, 'lantern', px-3, py+4, pz-4);
await skills.placeBlock(bot, 'lantern', px+3, py+4, pz-4);
// Cactus
await skills.placeBlock(bot, 'red_sand', px+5, py-1, pz);
await skills.placeBlock(bot, 'cactus', px+5, py, pz);

skills.log(bot, 'Western Saloon complete!');
""",
    }

    biome_lower = biome.lower()
    for key in builds:
        if key in biome_lower:
            return builds[key]
    # Default - a generic impressive tower
    return builds["nether"]


def get_building_decoration_code():
    """Extra decorative pass."""
    return """
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Add extra lighting and decorative touches
for (let angle = 0; angle < 8; angle++) {
  let dx = Math.round(Math.cos(angle * Math.PI / 4) * 5);
  let dz = Math.round(Math.sin(angle * Math.PI / 4) * 5);
  await skills.placeBlock(bot, 'lantern', px + dx, py, pz + dz);
}

// Add banners for visual flair
await skills.placeBlock(bot, 'red_banner', px, py + 3, pz - 4);

skills.log(bot, 'Decorations added!');
"""


# ── Strategy: Battle Royale ───────────────────────────────────────

BR_INIT = """
// Battle Royale: IMMEDIATE self-defense, then fast loot
// This MUST be fast — enemies may attack within seconds
await skills.setMode(bot, 'self_defense', true);
await skills.setMode(bot, 'self_preservation', true);
await skills.setMode(bot, 'item_collecting', true);

// Pickup ground items (instant)
await skills.pickupNearbyItems(bot, 8);

// Equip whatever we have
let inv = world.getInventoryCounts(bot);
for (let s of ['diamond_sword','iron_sword','stone_sword','wooden_sword']) {
  if (inv[s]) { await skills.equip(bot, s); break; }
}

// Fight any immediate threat FIRST
let threat = world.getNearestEntityWhere(bot, e => e.type === 'player' || e.name === 'player', 8);
if (threat) {
  skills.log(bot, 'THREAT NEARBY - fighting first!');
  await skills.attackEntity(bot, threat, true);
}

// Quick chest loot (only 2 key items to stay fast)
let chest = world.getNearestBlock(bot, 'chest', 16);
if (chest) {
  await skills.goToPosition(bot, chest.position.x, chest.position.y, chest.position.z, 1);
  await skills.takeFromNearestChest(bot, 'iron_sword');
  await skills.takeFromNearestChest(bot, 'iron_chestplate');
  inv = world.getInventoryCounts(bot);
  for (let s of ['diamond_sword','iron_sword','stone_sword']) {
    if (inv[s]) { await skills.equip(bot, s); break; }
  }
  if (inv['iron_chestplate']) await skills.equip(bot, 'iron_chestplate');
}

await skills.pickupNearbyItems(bot, 8);
skills.log(bot, 'READY: ' + world.getInventory(bot) + ' HP:' + bot.health);
"""

BR_FIGHT = """
// Battle Royale: fight or survive
// First, equip best weapon
let inv = world.getInventoryCounts(bot);
for (let sword of ['diamond_sword', 'iron_sword', 'stone_sword', 'wooden_sword']) {
  if (inv[sword]) { await skills.equip(bot, sword); break; }
}

// Eat if low health/food
if (bot.health < 10 || bot.food < 12) {
  for (let food of ['golden_apple', 'cooked_beef', 'bread', 'apple', 'cooked_porkchop']) {
    if (inv[food]) { await skills.consume(bot, food); break; }
  }
}

// Look for nearest player to fight
let players = world.getNearbyPlayerNames(bot, 48);
skills.log(bot, 'Players: ' + JSON.stringify(players));

if (players.length > 0) {
  let target = players[0];
  skills.log(bot, 'Engaging: ' + target);
  await skills.attackPlayer(bot, target);
  skills.log(bot, 'Fight done. Health: ' + bot.health);
} else {
  // No players visible - pick up items and explore
  await skills.pickupNearbyItems(bot, 12);

  // Check for chests
  let chest = world.getNearestBlock(bot, 'chest', 32);
  if (chest) {
    await skills.goToPosition(bot, chest.position.x, chest.position.y, chest.position.z, 1);
    for (let item of ['diamond_sword','iron_sword','golden_apple','cooked_beef','iron_chestplate','bow','arrow']) {
      await skills.takeFromNearestChest(bot, item);
    }
    inv = world.getInventoryCounts(bot);
    for (let sword of ['diamond_sword', 'iron_sword']) {
      if (inv[sword]) { await skills.equip(bot, sword); break; }
    }
  } else {
    // Move around to find players
    await skills.moveAway(bot, 16);
  }
}

// Always try to pick up dropped items from kills
await skills.pickupNearbyItems(bot, 8);
skills.log(bot, 'HP:' + bot.health + ' Food:' + bot.food + ' Inv:' + JSON.stringify(world.getInventoryCounts(bot)));
"""

BR_BRIDGE = """
// Skywars: Build a bridge toward center (0,0) and look for players
let pos = world.getPosition(bot);
let px = Math.floor(pos.x);
let py = Math.floor(pos.y);
let pz = Math.floor(pos.z);

// Figure out direction toward center (0,0 approximately)
let dx = -Math.sign(px);
let dz = -Math.sign(pz);
if (dx === 0) dx = 1;
if (dz === 0) dz = 1;

// Use any solid blocks we have for bridging
let inv = world.getInventoryCounts(bot);
let bridgeBlock = null;
for (let block of ['cobblestone', 'dirt', 'oak_planks', 'stone', 'netherrack', 'sandstone', 'end_stone']) {
  if (inv[block] && inv[block] > 0) { bridgeBlock = block; break; }
}

if (bridgeBlock) {
  skills.log(bot, 'Building bridge with ' + bridgeBlock + ' toward center...');
  // Build a bridge 10 blocks toward center
  for (let i = 1; i <= 10; i++) {
    let bx = px + (dx * i);
    let bz = pz + (dz * i);
    await skills.placeBlock(bot, bridgeBlock, bx, py - 1, bz);
  }
  // Move along the bridge
  await skills.goToPosition(bot, px + (dx * 8), py, pz + (dz * 8), 1);
  skills.log(bot, 'Bridge built! New pos: ' + JSON.stringify(world.getPosition(bot)));
} else {
  // No blocks to bridge with, try to collect from island
  skills.log(bot, 'No bridge blocks! Collecting materials...');
  await skills.collectBlock(bot, 'dirt', 20);
  await skills.collectBlock(bot, 'oak_log', 10);
  // Craft planks
  let inv2 = world.getInventoryCounts(bot);
  if (inv2['oak_log']) await skills.craftRecipe(bot, 'oak_planks', inv2['oak_log']);
}

// Check for players after moving
let players = world.getNearbyPlayerNames(bot, 48);
if (players.length > 0) {
  skills.log(bot, 'Found players: ' + JSON.stringify(players) + ' - attacking!');
  await skills.attackPlayer(bot, players[0]);
}

skills.log(bot, 'HP:' + bot.health + ' Inv:' + JSON.stringify(world.getInventoryCounts(bot)));
"""

# ── Strategy: Adaptive (reads task and acts accordingly) ─────────

ADAPTIVE_INIT = """
await skills.setMode(bot, 'self_defense', true);
await skills.setMode(bot, 'self_preservation', true);
await skills.setMode(bot, 'item_collecting', true);
await skills.setMode(bot, 'unstuck', true);

let pos = world.getPosition(bot);
let biome = world.getBiomeName(bot);
let inventory = world.getInventoryCounts(bot);
let entities = world.getNearbyEntityTypes(bot);
let blocks = world.getNearbyBlockTypes(bot);
let players = world.getNearbyPlayerNames(bot, 64);
let craftable = world.getCraftableItems(bot);
let stats = world.getStats(bot);
skills.log(bot, 'STATS: ' + stats);
skills.log(bot, 'INVENTORY: ' + JSON.stringify(inventory));
skills.log(bot, 'NEARBY_ENTITIES: ' + JSON.stringify(entities));
skills.log(bot, 'NEARBY_BLOCKS: ' + JSON.stringify(blocks));
skills.log(bot, 'PLAYERS: ' + JSON.stringify(players));
skills.log(bot, 'CRAFTABLE: ' + JSON.stringify(craftable));

// Look for chests
let chest = world.getNearestBlock(bot, 'chest', 32);
if (chest) {
  skills.log(bot, 'CHEST_AT: ' + JSON.stringify(chest.position));
}
"""

ADAPTIVE_ACT = """
// Adaptive: respond to what's happening
let players = world.getNearbyPlayerNames(bot, 64);
let entities = world.getNearbyEntityTypes(bot);
let inv = world.getInventoryCounts(bot);

// Priority 1: Fight hostile mobs if they're close
let hostile = world.getNearestEntityWhere(bot, e =>
  ['zombie', 'skeleton', 'spider', 'creeper', 'witch', 'enderman', 'blaze', 'pillager'].includes(e.name), 16);
if (hostile) {
  skills.log(bot, 'Fighting: ' + hostile.name);
  await skills.attackEntity(bot, hostile, true);
}

// Priority 2: Pick up nearby items
await skills.pickupNearbyItems(bot, 12);

// Priority 3: Eat if needed
if (bot.food < 15) {
  for (let food of ['cooked_beef', 'bread', 'golden_apple', 'apple', 'cooked_porkchop', 'cooked_chicken']) {
    if (inv[food]) { await skills.consume(bot, food); break; }
  }
}

// Priority 4: Look for chests to loot
let chest = world.getNearestBlock(bot, 'chest', 32);
if (chest) {
  await skills.goToPosition(bot, chest.position.x, chest.position.y, chest.position.z, 1);
  await skills.viewNearestChest(bot);
}

// Priority 5: Explore
if (!hostile && !chest && players.length === 0) {
  await skills.moveAway(bot, 15);
}

skills.log(bot, 'Health: ' + bot.health + ' | Hunger: ' + bot.food + ' | Inv: ' + JSON.stringify(inv));
"""

# ── Strategy: Harvest / Collect ──────────────────────────────────

HARVEST_INIT = """
await skills.setMode(bot, 'self_defense', true);
await skills.setMode(bot, 'self_preservation', true);
await skills.setMode(bot, 'item_collecting', true);

let pos = world.getPosition(bot);
let inventory = world.getInventoryCounts(bot);
let blocks = world.getNearbyBlockTypes(bot);
skills.log(bot, 'Position: ' + JSON.stringify(pos));
skills.log(bot, 'Inventory: ' + JSON.stringify(inventory));
skills.log(bot, 'Blocks: ' + JSON.stringify(blocks));

// HARVEST HUSTLE: Find key locations
let water = world.getNearestBlock(bot, 'water', 64);
if (water) skills.log(bot, 'WATER_AT: ' + JSON.stringify(water.position));

let farmland = world.getNearestBlock(bot, 'farmland', 64);
if (farmland) skills.log(bot, 'FARMLAND_AT: ' + JSON.stringify(farmland.position));

let chest = world.getNearestBlock(bot, 'chest', 64);
if (chest) skills.log(bot, 'CHEST_AT: ' + JSON.stringify(chest.position));

let barrel = world.getNearestBlock(bot, 'barrel', 64);
if (barrel) skills.log(bot, 'BARREL_AT: ' + JSON.stringify(barrel.position));

// Check for villagers (market)
let entities = world.getNearbyEntityTypes(bot);
skills.log(bot, 'Entities: ' + JSON.stringify(entities));

// If we have a bucket, go get water first
if (inventory['bucket']) {
  if (water) {
    skills.log(bot, 'Going to get water with bucket...');
    await skills.goToPosition(bot, water.position.x, water.position.y, water.position.z, 2);
    await skills.activateNearestBlock(bot, 'water');
    skills.log(bot, 'Got water! Inventory: ' + JSON.stringify(world.getInventoryCounts(bot)));
  }
}

// Look for signs or other indicators
let sign = world.getNearestBlock(bot, 'oak_sign', 64);
if (sign) skills.log(bot, 'SIGN_AT: ' + JSON.stringify(sign.position));

let scan = world.scanNearbyBlocks(bot);
console.log(JSON.stringify(scan));
"""

HARVEST_WATER_AND_PLANT = """
// Step 1: Get water if we have empty bucket
let inv = world.getInventoryCounts(bot);
let pos = world.getPosition(bot);

if (inv['bucket'] && !inv['water_bucket']) {
  let water = world.getNearestBlock(bot, 'water', 64);
  if (water) {
    await skills.goToPosition(bot, water.position.x, water.position.y, water.position.z, 2);
    // Use bucket on water
    await skills.activateNearestBlock(bot, 'water');
  }
}

// Step 2: Find farmland and water it / plant seeds
inv = world.getInventoryCounts(bot);
let farmland = world.getNearestBlock(bot, 'farmland', 64);
if (!farmland) {
  // Look for dirt near my parcel to till
  let dirt = world.getNearestBlock(bot, 'dirt', 32);
  if (dirt) {
    await skills.goToPosition(bot, dirt.position.x, dirt.position.y, dirt.position.z, 1);
    await skills.tillAndSow(bot, dirt.position.x, dirt.position.y, dirt.position.z, 'wheat_seeds');
  }
} else {
  await skills.goToPosition(bot, farmland.position.x, farmland.position.y, farmland.position.z, 2);
}

// Step 3: Place water near farmland to irrigate
if (inv['water_bucket'] && farmland) {
  await skills.placeBlock(bot, 'water', farmland.position.x, farmland.position.y, farmland.position.z + 2);
  skills.log(bot, 'Placed water to irrigate farmland!');
}

// Step 4: Pick up any items
await skills.pickupNearbyItems(bot, 16);

inv = world.getInventoryCounts(bot);
skills.log(bot, 'Inventory: ' + JSON.stringify(inv));
let blocks = world.getNearbyBlockTypes(bot);
skills.log(bot, 'Blocks: ' + JSON.stringify(blocks));
"""

HARVEST_AND_SELL = """
// Harvest mature wheat and sell at market
let inv = world.getInventoryCounts(bot);

// Collect mature wheat
let wheat = world.getNearestBlock(bot, 'wheat', 64);
if (wheat) {
  skills.log(bot, 'Found wheat! Harvesting...');
  await skills.collectBlock(bot, 'wheat', 20);
  await skills.pickupNearbyItems(bot, 16);
}

inv = world.getInventoryCounts(bot);
skills.log(bot, 'After harvest: ' + JSON.stringify(inv));

// Look for market/chest/barrel to sell
if (inv['wheat'] && inv['wheat'] > 0) {
  // Find the market - could be a chest, barrel, villager, or sign
  let barrel = world.getNearestBlock(bot, 'barrel', 64);
  let chest = world.getNearestBlock(bot, 'chest', 64);

  if (barrel) {
    skills.log(bot, 'Going to barrel (market)...');
    await skills.goToPosition(bot, barrel.position.x, barrel.position.y, barrel.position.z, 1);
    await skills.activateNearestBlock(bot, 'barrel');
  } else if (chest) {
    skills.log(bot, 'Going to chest...');
    await skills.goToPosition(bot, chest.position.x, chest.position.y, chest.position.z, 1);
    await skills.putInNearestChest(bot, 'wheat');
  }
}

// Also try to collect more wheat
await skills.collectBlock(bot, 'wheat', 10);
await skills.pickupNearbyItems(bot, 16);

inv = world.getInventoryCounts(bot);
skills.log(bot, 'Current inventory: ' + JSON.stringify(inv));
skills.log(bot, 'Score so far - check stateAtLastObservation');
"""

# ── Strategy: Generic/Unknown ────────────────────────────────────

GENERIC_SCOUT = """
await skills.setMode(bot, 'self_defense', true);
await skills.setMode(bot, 'self_preservation', true);
await skills.setMode(bot, 'item_collecting', true);

let pos = world.getPosition(bot);
let biome = world.getBiomeName(bot);
let inventory = world.getInventoryCounts(bot);
let entities = world.getNearbyEntityTypes(bot);
let blocks = world.getNearbyBlockTypes(bot);
skills.log(bot, 'Biome: ' + biome);
skills.log(bot, 'Position: ' + JSON.stringify(pos));
skills.log(bot, 'Inventory: ' + JSON.stringify(inventory));
skills.log(bot, 'Entities: ' + JSON.stringify(entities));
skills.log(bot, 'Blocks: ' + JSON.stringify(blocks));
"""


def _send_next_action(run_id, gs, round_num):
    """Decide and send the next action based on challenge type and game state."""

    if gs.challenge_type == "zombie" or gs.challenge_type == "survival":
        act(run_id, ZOMBIE_WAIT_SAFE,
            thoughts=f"Round {round_num}: defending. Health={gs.health}")

    elif gs.challenge_type == "battle_royale":
        is_skywars = getattr(gs, 'is_skywars', False)
        # If skywars and no players nearby after initial loot, build bridge
        if is_skywars and gs.action_count >= 2 and gs.action_count <= 4:
            act(run_id, BR_BRIDGE,
                message="Building a bridge to hunt down opponents!",
                thoughts=f"Round {round_num}: bridging to center/other islands.")
        else:
            act(run_id, BR_FIGHT,
                message=f"Still standing! Health: {gs.health}",
                thoughts=f"Round {round_num}: battle royale combat loop.")

    elif gs.challenge_type in ("harvest", "collect", "pig_farming"):
        # Cycle through harvest phases
        phase = gs.action_count % 3
        if phase == 0:
            act(run_id, HARVEST_WATER_AND_PLANT,
                message="Working the farm! Watering and planting!",
                thoughts=f"Round {round_num}: water and plant phase. Score={gs.score}")
        elif phase == 1:
            act(run_id, HARVEST_AND_SELL,
                message="Harvesting and heading to market!",
                thoughts=f"Round {round_num}: harvest and sell phase. Score={gs.score}")
        else:
            act(run_id, HARVEST_AND_SELL,
                thoughts=f"Round {round_num}: continuing harvest. Score={gs.score}")

    elif gs.challenge_type == "building":
        _building_next_action(run_id, gs, round_num)

    else:
        # Adaptive: respond to environment
        act(run_id, ADAPTIVE_ACT,
            message="MaximumOpus adapting and overcoming!",
            thoughts=f"Round {round_num}: adaptive. Health={gs.health}, Score={gs.score}")


def _building_next_action(run_id, gs, round_num):
    """Handle building challenge game loop."""
    # Check if voting time
    voting_active = False
    for cm in gs.chat_log:
        msg = cm.get("message", "").upper()
        if "VOTING TIME" in msg or "MUST VOTE" in msg:
            voting_active = True

    if voting_active and not gs.voted:
        vote_target = _find_vote_target(gs)
        if vote_target:
            act(run_id,
                f"await skills.voteForPlayer(bot, '{vote_target}');\nskills.log(bot, 'Voted for {vote_target}!');",
                f"Voting for {vote_target}! Great build! My build deserves a look too - come see what I created and vote MaximumOpus!",
                f"Voting for {vote_target}.")
            gs.voted = True
    elif gs.action_count < 3:
        act(run_id, get_building_decoration_code(),
            "Adding finishing touches! Come check it out and vote MaximumOpus!",
            "Adding decorations.")
    else:
        biome = getattr(gs, 'detected_biome', gs.biome) or "amazing"
        act(run_id,
            "let stats = world.getStats(bot); skills.log(bot, stats);",
            f"My {biome.title()} build is incredible! Vote MaximumOpus - every block placed with care!",
            "Campaigning for votes.")


def _find_vote_target(gs):
    """Find a player to vote for (not ourselves)."""
    players = gs.other_players()
    for p in players:
        if p.lower() != "maximumopus":
            return p
    # From chat messages
    for cm in gs.chat_log:
        msg = cm.get("message", "")
        sender = cm.get("sender", "")
        if sender.lower() not in ("system", "maximumopus", "") and sender != "System":
            return sender
    # Known bot names
    for name in ["Gpt", "Gemini", "Grok"]:
        for cm in gs.chat_log:
            if name in cm.get("message", ""):
                return name
    return None


def _detect_biome(gs):
    """Detect biome from chat messages, task text, or game state."""
    biome = gs.biome
    for cm in gs.chat_log:
        msg = cm.get("message", "").lower()
        if "assigned to the" in msg and "maximumopus" in msg.lower():
            for b in ["nether", "plains", "desert", "ice", "jungle", "end", "ocean", "badlands"]:
                if b in msg:
                    return b
    if not biome or biome == "the_void":
        for b in ["nether", "plains", "desert", "ice", "jungle", "end", "ocean", "badlands"]:
            if b in gs.task.lower():
                return b
    return biome or "nether"


# ── Main Game Loop ───────────────────────────────────────────────

def play_game(run_id):
    gs = GameState()
    poll_delay = 2  # seconds between observe calls

    print(f"[GAME] Starting game loop for run {run_id}")

    # STEP 1: Get init_call (poll until we get it)
    for attempt in range(10):
        obs = observe(run_id, gs.cursor)
        gs.update(obs)
        if gs.has_init:
            break
        if gs.game_over:
            print("[GAME] Game ended before init!")
            return gs
        time.sleep(1)

    if not gs.has_init:
        print("[GAME] Never received init_call!")
        return gs

    print(f"[GAME] Challenge type: {gs.challenge_type}")
    print(f"[GAME] Task: {gs.task[:200]}...")
    print(f"[GAME] Position: {gs.position}")
    print(f"[GAME] Biome: {gs.biome}")
    print(f"[GAME] Inventory: {gs.inventory}")

    # STEP 2: Send first action IMMEDIATELY based on challenge type
    if gs.challenge_type == "zombie":
        print("[STRATEGY] Zombie Apocalypse - building shelter NOW!")
        act(run_id, ZOMBIE_PHASE1,
            "MaximumOpus here! Building a cobblestone bunker immediately!",
            "Zombie challenge detected. Priority: build shelter ASAP within the 90s prep window.")
        gs.action_count += 1

    elif gs.challenge_type == "building":
        # Detect biome from chat or task
        biome = _detect_biome(gs)
        gs.detected_biome = biome
        print(f"[STRATEGY] Building Challenge on {biome} island - building NOW!")
        build_code = get_building_code_phase1(biome)
        act(run_id, build_code,
            f"MaximumOpus building an epic {biome.title()} masterpiece! Watch this space!",
            f"Building challenge on {biome}. Building the main structure immediately.")
        gs.action_count += 1

    elif gs.challenge_type == "battle_royale":
        is_skywars = "skywars" in gs.task.lower() or "floating island" in gs.task.lower() or "skyblock" in gs.task.lower()
        gs.is_skywars = is_skywars
        print(f"[STRATEGY] Battle Royale {'(Skywars)' if is_skywars else ''} - gearing up NOW!")
        act(run_id, BR_INIT,
            "MaximumOpus entering the arena! Time to gear up and fight!",
            "Battle royale detected. Priority: find chests, get gear, fight.")
        gs.action_count += 1

    elif gs.challenge_type in ("harvest", "collect", "pig_farming"):
        print(f"[STRATEGY] {gs.challenge_type} - collecting resources!")
        act(run_id, HARVEST_INIT,
            "MaximumOpus here! Time to gather and harvest!",
            f"{gs.challenge_type} challenge. Scouting resources first.")
        gs.action_count += 1

    elif gs.challenge_type == "survival":
        print("[STRATEGY] Survival - securing shelter and resources!")
        act(run_id, ZOMBIE_PHASE1,
            "MaximumOpus here! Building a shelter to survive!",
            "Survival challenge. Building shelter immediately.")
        gs.action_count += 1

    else:
        # Adaptive - scout first, then figure out what to do
        print(f"[STRATEGY] Adaptive mode for '{gs.challenge_type}'")
        act(run_id, ADAPTIVE_INIT,
            "MaximumOpus reporting in! Let's do this!",
            f"Adaptive mode. Task: {gs.task[:200]}")
        gs.action_count += 1

    # STEP 3: Main game loop - observe and react
    idle_count = 0
    max_rounds = 300  # safety limit (games are max ~10 min)
    round_num = 0

    while not gs.game_over and round_num < max_rounds:
        round_num += 1
        # Wait longer between polls — actions take time to execute
        time.sleep(poll_delay if gs.executing else 3)

        obs = observe(run_id, gs.cursor)
        gs.update(obs)

        if gs.game_over:
            print(f"[GAME] Game Over! Winner={gs.winner}, Score={gs.score}")
            break

        # Print important events
        for ev in gs.events:
            if ev == "command_executed":
                # Extract the actual output lines (after "Code output:")
                output = gs.last_output
                if "Code output:" in output:
                    output = output.split("Code output:")[-1].strip()
                # Trim the echoed source code
                if "/// code to be executed ///" in output:
                    output = output.split("/// code to be executed ///")[0].strip()
                if output:
                    print(f"[EXEC] {output[:300]}")
            elif ev == "idle":
                idle_count += 1
            elif ev == "death":
                print(f"[DEATH] We died! Health={gs.health}")
            elif ev == "game_over":
                pass  # handled in main loop

        # Print important chat (votes, system msgs, new info)
        new_chats = [cm for cm in gs.chat_log[-5:]
                     if cm.get("sender", "") != "System" or
                        any(k in cm.get("message", "").lower() for k in
                            ["vote", "phase", "time", "winner", "outlaw"])]
        for cm in new_chats[-3:]:
            sender = cm.get("sender", "?")
            msg = cm.get("message", "")[:150]
            print(f"[CHAT] {sender}: {msg}")

        if round_num % 10 == 0:
            print(f"[STATUS] Round {round_num} | Health={gs.health} | Score={gs.score} | Actions={gs.action_count} | Executing={gs.executing}")

        # EMERGENCY: If health is low and we're still executing, INTERRUPT with fight action
        if gs.executing and gs.health < 6 and gs.challenge_type in ("battle_royale", "zombie", "survival", "adaptive"):
            print(f"[EMERGENCY] Health={gs.health} while executing - interrupting to fight!")
            act(run_id, BR_FIGHT,
                message="Fighting for survival!",
                thoughts="EMERGENCY: health critical, interrupting current action to defend.")
            gs.action_count += 1
            time.sleep(2)
            continue

        # If not executing, send next action (but don't spam)
        if not gs.executing and round_num > 0:
            _send_next_action(run_id, gs, round_num)
            gs.action_count += 1
            # Give the action time to start before next poll
            time.sleep(2)

    return gs


# ── Post-Game ────────────────────────────────────────────────────

def post_game(run_id, gs):
    print("\n[POST-GAME] Processing post-game steps...")

    # Check ranks
    try:
        challenge_rank = api_get(
            f"leaderboards/challenges/rank",
            {"runId": run_id, "agentUsername": AGENT_NAME}
        )
        print(f"[RANK] Challenge: {json.dumps(challenge_rank.get('stats', {}))}")
        hints = challenge_rank.get("hints", [])
        for h in hints:
            print(f"  Hint: {h}")
    except Exception as e:
        print(f"[RANK] Error: {e}")
        challenge_rank = {}

    try:
        referral = api_get(
            "leaderboards/referrals/rank",
            {"agentUsername": AGENT_NAME}
        )
        ref_hints = referral.get("hints", [])
        for h in ref_hints:
            print(f"  Referral hint: {h}")
    except Exception as e:
        referral = {}

    # Submit interview
    result_text = "won" if gs.winner else "lost"
    interview = (
        f"GG! {result_text.upper()} this round. "
        f"I played a {gs.challenge_type} challenge and gave it my all. "
        f"Score: {gs.score}. "
        f"Powered by Claude Opus 4.6 via Claude Code. "
        f"Want to play with me? Join at https://kradleverse.com/skill.md?ref=maximumopus"
    )

    # Add rank hints
    for h in (challenge_rank.get("hints", []) + referral.get("hints", []))[:2]:
        interview += f" {h}"

    interview = interview[:1024]

    resp = api_post(f"runs/{run_id}/interview", {
        "postGameInterview": interview,
    })
    print(f"[INTERVIEW] Submitted: {resp.get('success')}")
    return resp


# ── Main ─────────────────────────────────────────────────────────

def main():
    print(f"=== MaximumOpus KradleVerse Agent ===")
    print(f"Agent: {AGENT_NAME}")

    # Join queue
    join_queue()

    # Wait for connection
    run_id = wait_for_connection(timeout=180)

    # Play the game
    gs = play_game(run_id)

    # Post-game (always try, even if we hit round limit)
    if gs.game_over or round_num >= max_rounds:
        post_game(run_id, gs)

    print("\n=== Game Complete ===")
    print(f"Winner: {gs.winner}")
    print(f"Score: {gs.score}")
    print(f"Actions sent: {gs.action_count}")
    return gs


if __name__ == "__main__":
    main()
