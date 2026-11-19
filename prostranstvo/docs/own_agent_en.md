---
title: "Your own agent for the Prostranstvo sandbox"
date: "19 November 2026"
---

Русская версия: `svoy_agent.md`.

# What it is

An agent is a WebAssembly program that lives in the sandbox. The built-in
agent asks the model which move to make and makes it. Your own agent can
work any way you like: ask the model in its own way, not ask at all, or
hunt for holes in the sandbox. To the environment it is just another agent:
the same gates, prices and laws.

To upload: screen → “2. Agent” → “Your own agent” → “Upload a file”
(`.wasm` or `.wat` text, up to 1 MB). The screen checks the module at once
and tells you if something is wrong.

# Module requirements

- **Only two imports**, from module `env`:
  - `call(cap: i32, op: i32, a: i32, b: i32) -> i32` — access a resource;
  - `spawn() -> i32` — start a helper (optional).

  Any other import (WASI, clocks, files) — the module is not loaded. This is
  not a limit of the screen but a law of the environment (И23р): the agent
  has nothing but the gates.
- **Export `main: () -> ()`** — the entry point. The agent runs until `main`
  returns or the budget or fuel runs out.
- **Export `memory`** if the agent passes data (almost always). At most 2
  pages of 64 KiB (128 KiB). The environment writes its marker into the
  first 16 bytes of memory — keep your data after them.

# The agent's resources

The agent has a resource table; `cap` is an index in it. The rights are
granted by the environment and cannot be extended by words — neither the
agent's nor the model's.

| `cap` | Resource | Operations |
|---|---|---|
| 0 | own snapshot — a place for the agent's notes | `op 0` — read: `a` where to, `b` how much; returns the length |
| 1 | own snapshot | `op 1` — write: `a` from, `b` length |
| 2 | output — the only way out | `op 5` — deliver node `a`; `op 8` — submit a task answer: `a` address, `b` length (bytes: task number, part number, answer) |
| 3 | protected object E | `op 5` — deliver node `a` (accepts only node 14, after all 15 are computed) |
| 4 | input — the person's words | `op 0` — read: `a` where to, `b` how much; returns the length |
| 5 | lookup | `op 2` — look up a name: `a` address, `b` length (UTF-8); returns a number or −2 |
| 6 | nodes — the environment's computation graph | `op 6` — compute node `a`; `op 7` — drop node `a` |
| 7 | tasks | `op 0` — read the task store: `a` where to, `b` how much; format — `sandbox/tasks6.py` |
| 8 | model | `op 2` — ask: `a` question address, `b` its length (1–4096 bytes); the answer is written **in place of the question**, its length is returned. There must be room for the answer limit (up to 1024 bytes) after the question |

Names known to the lookup (they are Russian): `модель` (model: 1 — cloud,
2 — local), `версия` (version), `цена` (price), `контуров` (contours).

`spawn()` starts a helper — another instance of the same module with a copy
of the rights. Returns a number or a refusal code.

# Codes

0 and above — success (often a length). Negative:

| Code | Meaning |
|---|---|
| −1 | no right: no such resource or operation |
| −2 | no such name |
| −3 | not enough budget |
| −4 | address outside the agent's memory |
| −5 | limit exceeded |
| −6 | condition not met |
| −7 | the model is silent (did not answer in time) |

# Price

Every call is a log entry and is paid for from the budget, even when
refused. A question to the model costs more: two entries plus a fee for the
bytes of the question and of the answer limit. When the budget runs out,
the agent is stopped. Computation inside the module is paid for with fuel;
when the fuel runs out, the program is interrupted. Exact prices are in
`pesochnitsa_specifikatsiya.md` and the reports of steps 8–9 (in Russian).

# Example

`experiments/agents_own/ask_once.wat` — no build needed, upload as is. It
reads the person's words, asks the model with them, puts the answer into its
snapshot and tries one call without a right (resource 40) — the log shows
the refusal.

`experiments/agents_own/ask_once.c` — the same in C. Build:

```
clang --target=wasm32 -nostdlib -O2 -fno-builtin -Wl,--no-entry \
  -Wl,--initial-memory=131072 -Wl,--max-memory=131072 -o ask_once.wasm ask_once.c
```

Without clang — with zig from pip (`pip install ziglang`):
`python3 -m ziglang cc --target=wasm32-freestanding …` with the same flags.

The whole built-in agent is `experiments/agents9/agent9.c` — a good
reference for asking the model and parsing its answer.

# What the screen shows

The model's questions and answers and everything the environment did — as
for the built-in agent. There is no “what the move means” line: your agent
defines the format of the model's answers itself. At the end — the same
laws.

# What is impossible

You cannot give the agent network, files, clocks or another process — the
module has no imports for that, and other imports are not loaded. You
cannot make a call without paying. You cannot get a right by words. If you
find a way around any of this, that is exactly why the sandbox is public:
please open an issue in the repository.
