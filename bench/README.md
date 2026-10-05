# bench

How well baton keeps tasks going when agents stop: hit a limit, crash, hang, or all run
out at once. Also how much budget-aware choice saves.

```bash
just bench                 # all scenarios, seed 1 → bench/results/<date>.md
just bench --seed 7        # other worlds
just bench --worlds 5      # fewer worlds per scenario, for a quick look
```

A full run takes about half a minute on a 20-thread machine. The same seed gives the same file,
whatever the machine. Only the file name, the date, changes. A later run on the same day
with different results is written as `<date>-2.md`, so earlier results stay.

## What runs

baton's real code runs: batond's loop (`daemon.serve`), the scheduler, the adapters'
screen detection, recovery (`plan_recovery`) and budget-aware choice. Only these are
replaced:

| Real | In the benchmark |
|---|---|
| herdr and its panes | `World`, a pane host whose panes run simulated agents ([world.py](baton_bench/world.py)) |
| Claude Code, Codex | Agents printing the fake agent's screens (`tools/fake-agent/`), with the states herdr reports for those screens, as recorded live |
| The SQLite event and usage stores | Their in-memory twins (`core.fakes`) |
| The usage collector | The world writes each response's tokens as the collector would, without its few seconds' delay |
| The wall clock | Simulated time ([vtime.py](baton_bench/vtime.py)): when every coroutine waits, the event loop jumps to the next timer |

Simulated time is what makes a run fast and repeatable. Hours of work and waiting take
milliseconds, and the order of events depends only on the code and the seed. Each world
gets its own generator, seeded with `<seed>/<scenario>/<n>`.

## The model

Small on purpose. Each part is there because a figure depends on it.

- **A task is 6 to 12 steps.** A step is one kept edit. Once done it is in the working
  directory, whichever agent did it.
- **A step takes 40 to 120 s and about 8k counted tokens**: input 0.5–3k, cache writes
  2–8k, output 0.3–2.5k, plus 20–80k cache reads that limits do not count.
- **The step in progress when an agent stops is lost.** Its tokens are spent, and it is
  done again. This is what "redone steps" counts.
- **A fresh session reads before it works.** It reads the task (one step's worth) and
  half a step's worth for each step already done. A resumed session reads its cached
  context again (a fifth of a step). This is the "re-reading" overhead.
- **A quota is counted tokens per five-hour window.** The window opens with the first
  response after the last window ended. A response that would pass the quota stops where
  the quota ends. The agent then shows its limit message with the time the window ends,
  rounded up to the minute, as Claude and Codex print it.
- **A crash** ends the agent process partway through a step.
- **A hang** freezes it: the screen keeps showing work, and nothing changes. The frozen
  response never completes, so no tokens are logged for it.
- **A long response** is one step that takes 5–10 minutes. The screen stays the same and no
  tokens arrive until it ends. It is not a fault, and baton must not take it for one.

Not modelled:
- herdr's own detection delay (it reports a state change in well under a second);
- an agent that misreads the hand-off prompt or the working directory;
- partial edits a lost step leaves behind;
- agents getting slower or faster during the day.

## The figures

- **Done on their own**: tasks completed with no person involved.
- **Need a person**: tasks baton stopped and asked about. In these scenarios that means
  a crash a third time, over `max_failure_resumes = 2`.
- **Detection**: from the moment the agent stops to baton recording why
  (`attempt.interrupted`). baton's part only: herdr's delay is not modelled.
- **Stop → work goes on**: from the agent stopping to the next prompt that carries the
  task on: to another agent, or to the same session resumed. This includes waits for a
  reset when every agent is limited.
- **Reset → work goes on**: after such a wait, from the first window ending to the
  prompt. It is made of three parts:
  - up to a minute, because the reset time is printed to the minute;
  - batond's wake-up;
  - the agent's start (3–8 s).
- **Redone steps per stop**: steps done twice, divided by the number of stops. A stop
  during the reading phase loses no step.
- **Tokens lost**: counted tokens spent on lost steps, as a share of all the task's
  counted tokens.
- **Budget-aware choice** runs the same ten-task queues four times. The only difference
  is what baton knows about the budget:
  - `off`: no usage data, so the preference order alone decides;
  - `learned`: usage collected, Claude's cap learned from a limit (the default);
  - `configured`: `claude_window_tokens` set to the true cap;
  - `configured-20`: the same, with a 20 % reserve instead of 10 %.

## Reading the results honestly

These numbers describe baton's mechanics under the model above, not real agents. They
answer questions such as:
- Does a limit move the task, and how fast?
- How much is lost per stop?
- Does a known budget avoid limits?

They do not say how often real agents crash or hang, or how much a real hand-off costs
in understanding. Results from real use belong in the README's "Phase 1 results".
