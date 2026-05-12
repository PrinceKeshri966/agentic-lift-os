"""
╔══════════════════════════════════════════════════════════════════════╗
║  LIFT OS — Multi-Agent Elevator Dispatch Simulation                  ║
║  FastAPI + asyncio backend  │  WebSocket real-time state streaming   ║
╚══════════════════════════════════════════════════════════════════════╝

Architecture:
  ┌─ Agent 1: UserSimulator  ─ generates external + internal requests
  ├─ Agent 2: Dispatcher     ─ SCAN algorithm + cost-function assignment
  ├─ 3 × Lift                ─ independent async movement loops
  └─ WebSocket broadcast     ─ pushes JSON state to all browser clients
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from enum import Enum
from typing import List, Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# ─────────────────────────────────────────────────────────────────────
# Logging — pretty console output mirrors the web log panel
# ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lift_os")


# ═════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═════════════════════════════════════════════════════════════════════
MIN_FLOOR: int = -1          # Basement
MAX_FLOOR: int = 5           # Top floor
FLOORS: List[int] = list(range(MIN_FLOOR, MAX_FLOOR + 1))   # 7 floors

TICK_INTERVAL: float      = 0.55   # seconds per one-floor movement
BROADCAST_INTERVAL: float = 0.25   # WebSocket push cadence
SIM_MIN_DELAY: float      = 1.2    # min seconds between simulator bursts
SIM_MAX_DELAY: float      = 3.8    # max seconds between simulator bursts
MAX_LIFT_LOAD: int        = 8      # max passengers per lift


# ═════════════════════════════════════════════════════════════════════
#  ENUMS
# ═════════════════════════════════════════════════════════════════════
class Direction(str, Enum):
    UP   = "UP"
    DOWN = "DOWN"
    IDLE = "IDLE"


# ─────────────────────────────────────────────────────────────────────
# Global shared state — log buffer + WebSocket client registry
# ─────────────────────────────────────────────────────────────────────
log_buffer: deque[dict]  = deque(maxlen=200)
ws_clients: Set[WebSocket] = set()

# Counters exposed on the frontend
_stats = {"requests": 0, "arrivals": 0, "uptime_start": time.time()}


def _floor_label(floor: int) -> str:
    """Return human-readable floor label (-1 → B1, 0 → G, else str)."""
    if floor == -1:
        return "B1"
    if floor == 0:
        return "G"
    return str(floor)


def add_log(source: str, message: str) -> None:
    """Append a structured log entry and echo to terminal."""
    entry = {
        "source": source,
        "msg":    message,
        "ts":     time.strftime("%H:%M:%S"),
    }
    log_buffer.append(entry)
    logger.info("[%-12s] %s", source, message)


# ═════════════════════════════════════════════════════════════════════
#  CLASS: Lift
#  Represents one elevator car.  Thread-safe via asyncio.Lock.
#  Movement follows the SCAN (elevator) algorithm:
#    • While going UP  → service all targets above current floor first
#    • While going DOWN → service all targets below current floor first
#    • Reverse only when no more targets exist in current direction
# ═════════════════════════════════════════════════════════════════════
class Lift:
    def __init__(self, lift_id: int, initial_floor: int = 0) -> None:
        self.lift_id  = lift_id
        self.floor    = initial_floor
        self.direction: Direction = Direction.IDLE
        self.targets: List[int]   = []   # pending stop floors
        self.load: int            = 0    # passengers aboard
        self._lock = asyncio.Lock()

    # ── Public: add a floor to the stop list ─────────────────────────
    def add_target(self, floor: int) -> None:
        if not (MIN_FLOOR <= floor <= MAX_FLOOR):
            return
        if floor not in self.targets:
            self.targets.append(floor)
        self._recompute_direction()

    # ── Internal: SCAN next-target selection ─────────────────────────
    def _next_target(self) -> Optional[int]:
        """Return the next floor to travel toward, respecting SCAN order."""
        if not self.targets:
            return None

        if self.direction == Direction.IDLE:
            # Closest floor wins when idle
            return min(self.targets, key=lambda f: abs(f - self.floor))

        if self.direction == Direction.UP:
            above = [f for f in self.targets if f >= self.floor]
            if above:
                return min(above)          # continue upward pass
            # Exhausted upward pass → reverse; pick highest remaining
            return max(self.targets)

        # Direction.DOWN
        below = [f for f in self.targets if f <= self.floor]
        if below:
            return max(below)              # continue downward pass
        return min(self.targets)           # exhausted downward pass → reverse

    # ── Internal: sync direction to next target ───────────────────────
    def _recompute_direction(self) -> None:
        nxt = self._next_target()
        if nxt is None:
            self.direction = Direction.IDLE
        elif nxt > self.floor:
            self.direction = Direction.UP
        elif nxt < self.floor:
            self.direction = Direction.DOWN
        else:
            self.direction = Direction.IDLE

    # ── Called every TICK: move one floor toward next target ──────────
    async def tick(self) -> None:
        async with self._lock:
            if not self.targets:
                self.direction = Direction.IDLE
                return

            nxt = self._next_target()
            if nxt is None:
                return

            # --- move one floor ---
            if self.floor < nxt:
                self.floor += 1
                self.direction = Direction.UP
            elif self.floor > nxt:
                self.floor -= 1
                self.direction = Direction.DOWN
            # (if floor == nxt we stay and process arrival below)

            # --- arrival at a target floor ---
            if self.floor in self.targets:
                self.targets.remove(self.floor)
                _stats["arrivals"] += 1

                # Simulate passengers alighting and boarding
                alight = random.randint(0, min(self.load, 3))
                board  = random.randint(0, min(3, MAX_LIFT_LOAD - (self.load - alight)))
                self.load = max(0, self.load - alight + board)

                next_str = (
                    f"→ next stop {_floor_label(self.targets[0])}"
                    if self.targets else "→ IDLE"
                )
                add_log(
                    f"Lift {self.lift_id}",
                    f"Arrived {_floor_label(self.floor)} | "
                    f"{alight} alight, {board} board (load={self.load}) {next_str}",
                )

            self._recompute_direction()

    # ── Serialise state for JSON broadcast ────────────────────────────
    def to_dict(self) -> dict:
        return {
            "id":        self.lift_id,
            "floor":     self.floor,
            "direction": self.direction.value,
            "targets":   sorted(self.targets),
            "load":      self.load,
        }


# ═════════════════════════════════════════════════════════════════════
#  CLASS: Dispatcher
#  Receives (floor, direction) requests from the queue.
#  Assigns the lowest-cost lift using:
#    cost = distance × direction_penalty + load_penalty
# ═════════════════════════════════════════════════════════════════════
class Dispatcher:
    def __init__(self, lifts: List[Lift]) -> None:
        self.lifts = lifts
        self.queue: asyncio.Queue = asyncio.Queue()

    # ── Cost function: lower = more desirable assignment ─────────────
    def _cost(self, lift: Lift, req_floor: int, req_dir: Direction) -> float:
        distance     = abs(lift.floor - req_floor)
        load_penalty = lift.load * 0.45        # penalise crowded lifts

        if lift.direction == Direction.IDLE:
            return distance + load_penalty      # cheapest — idle lifts ideal

        # Lift heading same direction AND will naturally pass req_floor
        if lift.direction == req_dir:
            if lift.direction == Direction.UP and lift.floor <= req_floor:
                return distance * 0.4 + load_penalty   # on-the-way bonus
            if lift.direction == Direction.DOWN and lift.floor >= req_floor:
                return distance * 0.4 + load_penalty

        # Worst case: lift must reverse or is moving away
        return distance * 2.8 + load_penalty + 7.0

    # ── Assign best lift, add target, emit log ────────────────────────
    def assign(self, req_floor: int, req_dir: Direction) -> Lift:
        best      = min(self.lifts, key=lambda l: self._cost(l, req_floor, req_dir))
        cost_val  = self._cost(best, req_floor, req_dir)
        best.add_target(req_floor)
        _stats["requests"] += 1

        add_log(
            "Dispatcher",
            f"Lift {best.lift_id} (Floor {_floor_label(best.floor)}, "
            f"{best.direction}) → Floor {_floor_label(req_floor)} "
            f"[{req_dir}] cost={cost_val:.1f}",
        )
        return best

    # ── Background coroutine: drain the request queue ─────────────────
    async def run(self) -> None:
        while True:
            try:
                req_floor, req_dir = await asyncio.wait_for(
                    self.queue.get(), timeout=0.15
                )
                self.assign(req_floor, req_dir)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.04)


# ═════════════════════════════════════════════════════════════════════
#  CLASS: UserSimulator  (Agent 1)
#  Generates realistic human traffic:
#    • External requests: floor hall-call buttons (UP / DOWN)
#    • Internal requests: in-cab destination buttons
#    • Random bursts to simulate rush-hour
#    • Edge constraints: B1 → UP only, Floor 5 → DOWN only
# ═════════════════════════════════════════════════════════════════════
class UserSimulator:
    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher

    async def run(self) -> None:
        # Warm-up: seed a handful of requests so the UI is active immediately
        add_log("System", "Seeding initial traffic...")
        for floor in [0, 2, 4, -1, 3]:
            dir_ = (
                Direction.UP   if floor == MIN_FLOOR else
                Direction.DOWN if floor == MAX_FLOOR else
                random.choice([Direction.UP, Direction.DOWN])
            )
            await self.dispatcher.queue.put((floor, dir_))
            await asyncio.sleep(0.08)

        # Main simulation loop
        while True:
            await asyncio.sleep(random.uniform(SIM_MIN_DELAY, SIM_MAX_DELAY))

            # Rush-hour burst: 1–3 simultaneous floor requests
            burst = random.choices([1, 2, 3], weights=[0.55, 0.30, 0.15])[0]
            if burst > 1:
                add_log("Simulator", f"── Rush burst: {burst} simultaneous requests ──")

            for _ in range(burst):
                floor = random.choice(FLOORS)

                # Edge-case constraint enforcement
                if floor == MIN_FLOOR:
                    direction = Direction.UP
                elif floor == MAX_FLOOR:
                    direction = Direction.DOWN
                else:
                    direction = random.choice([Direction.UP, Direction.DOWN])

                add_log(
                    "Simulator",
                    f"User at Floor {_floor_label(floor)} presses {direction}.",
                )
                await self.dispatcher.queue.put((floor, direction))

                # After a short pause, simulate the same user also pressing
                # an in-cab destination button (internal request)
                await asyncio.sleep(random.uniform(0.05, 0.35))
                dest = random.choice([f for f in FLOORS if f != floor])

                # Assign internal request to the lift nearest that floor
                best = min(
                    self.dispatcher.lifts,
                    key=lambda l: abs(l.floor - floor),
                )
                best.add_target(dest)
                add_log(
                    "Simulator",
                    f"Passenger inside Lift {best.lift_id} "
                    f"selects Floor {_floor_label(dest)}.",
                )


# ═════════════════════════════════════════════════════════════════════
#  CLASS: Building  — top-level orchestrator
# ═════════════════════════════════════════════════════════════════════
class Building:
    def __init__(self) -> None:
        # Spread lifts across different floors for visual interest at start
        self.lifts      = [Lift(1, 0), Lift(2, 3), Lift(3, -1)]
        self.dispatcher = Dispatcher(self.lifts)
        self.simulator  = UserSimulator(self.dispatcher)

    async def run_lift_loop(self) -> None:
        """Tick every lift forward by one floor per TICK_INTERVAL."""
        while True:
            await asyncio.gather(*[lift.tick() for lift in self.lifts])
            await asyncio.sleep(TICK_INTERVAL)

    def get_state(self) -> dict:
        uptime = int(time.time() - _stats["uptime_start"])
        return {
            "lifts":     [l.to_dict() for l in self.lifts],
            "logs":      list(log_buffer)[-35:],
            "min_floor": MIN_FLOOR,
            "max_floor": MAX_FLOOR,
            "stats": {
                "requests": _stats["requests"],
                "arrivals": _stats["arrivals"],
                "uptime":   uptime,
            },
        }


# ═════════════════════════════════════════════════════════════════════
#  FastAPI app + WebSocket broadcast
# ═════════════════════════════════════════════════════════════════════
app      = FastAPI(title="Lift OS")
building = Building()


@app.on_event("startup")
async def _startup() -> None:
    """Kick off all background coroutines when the server starts."""
    add_log("System", "Lift OS online — 3 lifts active, floors B1 → 5.")
    asyncio.create_task(building.run_lift_loop(),      name="lift_loop")
    asyncio.create_task(building.dispatcher.run(),     name="dispatcher")
    asyncio.create_task(building.simulator.run(),      name="simulator")
    asyncio.create_task(_ws_broadcast_loop(),          name="broadcast")


async def _ws_broadcast_loop() -> None:
    """Push full state JSON to every connected browser client."""
    global ws_clients  # <--- YEH LINE ADD KARNI HAI
    while True:
        if ws_clients:
            payload = json.dumps(building.get_state())
            dead: Set[WebSocket] = set()
            for ws in list(ws_clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            ws_clients -= dead
        await asyncio.sleep(BROADCAST_INTERVAL)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    ws_clients.add(websocket)
    # Send current state immediately on connect so the page isn't blank
    await websocket.send_text(json.dumps(building.get_state()))
    try:
        while True:
            # Receive keeps the connection alive; client sends periodic pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(websocket)


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    with open("index.html", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)