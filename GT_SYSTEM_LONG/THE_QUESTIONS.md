# The Questions

### A climb toward execution certainty

> A test is a question made executable. A suite is only as good as the questions someone thought to ask. And questions have *altitude* — some ask whether the machine runs, some ask whether it is right, and a rare few ask what "right" even means. This document is a climb. Each altitude asks a deeper question than the one below it. We do not stop until the questions run out — and then we look at where we landed.

---

## Altitude 0 — Does it run?

The ground floor. The happy path. Necessary, never sufficient.

- Does a single bot place a bracket, fill, stop out, and re-enter?
- Do 32 bots run concurrently without stepping on each other?
- Does it survive a full trading session start to finish?

If this is all you test, you have a demo, not a system. Everyone passes Altitude 0. Nobody at the firms you admire is impressed by it.

---

## Altitude 1 — Does it survive when something breaks?

This is where most "serious" systems stop. It is *robustness*. It is the 50 questions you already mapped — disconnects, restarts, corrupted state, orphan positions, partial fills. They matter enormously. But notice what they all share: they ask **"does it stay alive?"** None of them ask **"is what it did correct?"**

A system can survive every fault on this list and still quietly lose money on every trade. Survival is the floor of trustworthiness, not the ceiling.

The questions here you've already mapped. Keep them. Then climb.

---

## Altitude 2 — Is it *correct*, not merely alive?

The first altitude that separates engineers from operators. A live-but-wrong system is more dangerous than a dead one, because it looks healthy while it bleeds.

**Numerical truth**
- Over a million price updates, does floating-point error accumulate in PnL, exposure, or average cost? Should price math be integer-cents or `Decimal` rather than float?
- Does tick-rounding ever round a stop *against* you? Round-half-even vs round-half-up at the penny — which, and is it consistent everywhere?
- Does the **sum of per-bot PnL equal the account-level PnL**, to the penny, always? (A conservation law. If it ever doesn't, something is silently wrong.)
- What is the smallest position economically worth holding — where does commission exceed expected edge?
- Does a **stock split or dividend** mid-position corrupt your cost basis?
- Sub-dollar tick regime: a stock crossing $1.00 changes its minimum tick. Does the engine follow?

**Economic truth**
- Are fills happening at *sane* prices, or is the engine silently accepting adverse fills it should reject?
- Over 10,000 cycles, is the realized **slippage distribution** what your model assumed? A drift here is a money leak no crash-test will ever find.
- If commission returns negative, zero, or 10× expected, does PnL stay sane?

This altitude asks: *is the system making money-correct decisions?* — which is, after all, the entire point of the firm.

---

## Altitude 3 — Is it deterministic, and can you explain it afterward?

The altitude Jane Street builds its culture on. A system you cannot reproduce is a system you cannot trust, because you can never prove why it did what it did.

- Replay the exact same input event stream — do you get **bit-for-bit identical output**? Or does hidden nondeterminism (dict ordering, async scheduling, a stray `time.time()` in the decision path) leak in?
- Can you **reconstruct full engine state from the audit log alone**, with zero live access? (Delete the state file, rebuild from events, assert identical.)
- After an incident at 14:32:07.412, can you answer *"why did it place that order"* from logs alone — no debugger, no live system?
- Is every decision a **pure function of (state, event)**? Or does wall-clock, randomness, or an external read contaminate it?
- Does **logging itself change behavior**? (The Heisenbug test — turn logging off, does it still pass?)
- Two engineers, same inputs, same seed — identical results?

If you cannot reconstruct a decision, you cannot defend it to a risk committee, a regulator, or yourself at 3am.

---

## Altitude 4 — What is *true*?

Here the questions stop being about code and start being about epistemology. Every execution system juggles competing claims about reality and must decide which to believe.

- You have **engine-truth** and **broker-truth**. But there is a third: **exchange-truth** — what actually happened in the market. When all three disagree, what is your **source-of-truth hierarchy**?
- What if the **broker itself is wrong or lagged** — reports a position you know is stale? You currently treat the broker as ground truth. Is that *always* correct?
- Two "authoritative" sources disagree and **both claim to be current**. What is the tiebreak — halt, alert, or pick one?
- For every piece of state, what is its **"as-of" time**? When did it become true, versus when did *you learn* it was true? (These are different clocks, and conflating them causes silent bugs.)
- A trade is **busted or corrected hours later** by the exchange. Does your system survive a fill being un-done after the fact?
- After reconnect you replay *fills* — but do you replay the **order-state changes** you missed? Cancels, modifies, rejects that happened in the gap? A fill is not the only thing that changes while you were blind.

This is the altitude where you realize "the position" is not a fact — it is a *claim*, and your job is to adjudicate claims.

---

## Altitude 5 — Who guards the guardians?

You build safety nets. This altitude asks what catches the safety net when *it* fails — recursive safety, the discipline that survived contact with your own live incident (the Ctrl+C that left nine positions open).

- What if the thing meant to **flatten you also fails**? Who flattens when the flattener dies?
- What if the **alert system is down** at the exact moment of the incident? Is there a *second, independent* path to scream?
- Your invariant sweep is a net. **What is the net for the net?** If the sweep has a bug, what catches it?
- Is there a **dead-man's switch** — if the engine stops heart-beating, does something *external* flatten and halt? (The engine cannot be trusted to detect its own death.)
- You deploy a fix; the fix has a bug; positions are open. **How fast can you roll back?**
- When did you **last pull the kill switch** to confirm it works? (An untested kill switch is a decoration.)

A safety mechanism you have never triggered is a hypothesis, not a guarantee.

---

## Altitude 6 — Never trust the counterparty's bytes

The broker is not malicious, but it is not your friend either. It sends what it sends. The question is whether your engine treats every inbound message as potentially hostile.

- A fill arrives for an **order you never placed**. (You reject phantom SELLs — what about a phantom BUY? A fill for the *wrong symbol*? A fill bearing a clientId that isn't yours?)
- A fill arrives with a **future timestamp**, or one *before the order existed*.
- The **same execId twice** — or worse, two *different* execIds for the *same* economic fill.
- A position appears in a symbol **not in your universe at all**.
- `nextValidId` **goes backwards** after a reconnect.
- Broker responses arrive **faster than physically possible** — stale cache served as live, sequence-number gaps in the stream.

The engine should be able to say, of any inbound message: *"this is impossible; I refuse it and I alert"* — rather than dutifully corrupting itself to match a lie.

---

## Altitude 7 — Time, the quietest adversary

Time is assumed to be monotonic, uniform, and singular. It is none of those. Almost every execution system has a time bug it hasn't found yet.

- A **leap second**. An **NTP step backward** (time briefly goes negative). Monotonic clock vs wall clock in the same decision.
- A **DST transition** mid-position. Which timezone is "daily loss" measured in — and does it shift under DST?
- An **exchange holiday or half-day** the engine didn't know about.
- The **RTH open/close boundary** crossing mid-cycle.
- Timer coalescing under load — your "every 10s" health check actually fires every 14s when the box is busy. Does the logic assume punctuality it doesn't have?

---

## Altitude 8 — Scale, correlation, and the herd

One bot is a unit test. Thirty-two correlated bots in a single news event is a different animal entirely.

- A **single news event hits all 32 symbols at once** — every bot stops out in the same 200ms. Does the combined order rate breach the broker/exchange throttle?
- **Correlation risk**: positions you modeled as independent all move together. Is your exposure cap *portfolio-level* and *atomic*, or naively per-bot?
- Under load, can a **slow bot starve the others** through a shared connection, thread pool, or the GIL?
- Does the **audit writer block the trading path** when disk I/O backs up? (If logging stalls, does trading stall?)
- Is the event queue **bounded**, and what's the **drop policy** when it overflows — oldest, newest, or block? Each is a different correctness decision.

---

## Altitude 9 — The human in the loop

Systems fail; so do the people operating them at 3am on no sleep. You lived this. A trustworthy system assumes its operator is fallible.

- An operator **fat-fingers a command** (you sent Ctrl+C to a live fleet). Does one wrong keystroke cause unbounded harm, or is it contained?
- **Two operators issue conflicting commands** at once.
- Does a **runbook exist**, and has anyone *executed it under pressure* — or is it fiction written once and never rehearsed?
- Can a tired human **understand the alert** in five seconds, or does it require archaeology?
- The scariest ambiguity of all: can you distinguish **"no trades because there's no signal"** from **"no trades because I'm silently broken"**? Silence is the most dangerous state a trading system can be in.

---

## Altitude 10 — Specification: what does "correct" even mean?

The thinnest air, where the best engineers live. You cannot test toward a target you have not defined.

- Is there a **written specification of "correct"** that exists *independently of the code*? (If correctness lives only inside the implementation, your tests merely check the code against itself — a tautology, not a proof.)
- Could the **state machine be model-checked** to prove that no reachable state is ever *"holding a position with no protective order"*? (TLA+, or a property checker, can prove this for *all* paths — something no finite test suite can.)
- Are your invariants **machine-checkable assertions**, or English prose in a document that drifts out of date?
- Over a long run, does **behavior itself drift**? Hour 1 versus hour 50 — same fill rate, same latency, same memory? Two independent long runs — statistically equivalent?

---

## The summit — the questions about the questions

At the top, the climb turns inward. The mark of someone who belongs at the firms you named is not a longer list — it is the question that *reframes* the list:

1. **What is my definition of correct?** — If you can't state it precisely, no test can check it.
2. **How do I know my tests test the right thing?** — Break the code on purpose; confirm a test goes red. A green suite over dead assertions is worse than no suite.
3. **What is the single worst thing that can happen — and have I tested *that* first?** — Tail-risk before breadth.
4. **What am I *assuming* about the broker, the market, the OS — that I have never verified?** — Every unexamined assumption is an untested test wearing a disguise.
5. **If this fails at 3am with nobody watching, what happens?** — The autonomy test.
6. **What can I *not* test — and what is my containment for it?**

That last one is the whole game.

---

## What we do with the untestable

You said it yourself, early and correctly: *everything is not testable.* This is not a limitation to apologize for — it is the central design constraint of every real trading system. You will **never** test your way to 99.99%. The market's space of behaviors is infinite; your test suite is finite. The gap is permanent.

So the discipline for the untestable is not testing. It is **containment**, and it has four pillars:

1. **Bound the blast radius.** You can't test gap-down-during-halt-during-disconnect — so cap max loss per position, per bot, per portfolio. The unforeseen failure is survivable because its *cost* is bounded, even when its *cause* is unknown.

2. **Assert invariants live, in production.** The same checks your tests run, run *continuously against reality*. `engine_position == broker_position` after every event — in the live system, not just the test. The untestable failure trips a live assertion the instant it occurs.

3. **Trip circuit breakers automatically.** Three anomalies in a row → halt. Drift detected → halt. Latency past threshold → halt. The system does not need to *understand* the novel failure to *stop* in the face of it.

4. **Guarantee reconstructability.** When the unforeseen thing happens — and it will — you may not have prevented it, but you must be able to *explain* it afterward, from logs alone. Yesterday's unexplained incident is tomorrow's new test (Altitude 1 grows by one).

The senior sentence, the one to carry: *"I cannot test that failure, so instead I bound its cost, assert against it live, halt automatically when it appears, and log enough to reconstruct it. The untestable becomes survivable."*

---

## Where this lands

Three things make an execution system trustworthy with real money. Notice that only one of them is "tests."

| Pillar | The question it answers | How far it scales |
| --- | --- | --- |
| **A specification of correct** | *What are we even trying to do?* | Bounds everything below it |
| **Exhaustive testing of the testable** | *Does it do that, under every fault we can imagine?* | Plateaus near 99.5% — finite suite, infinite world |
| **Containment of the untestable** | *When it fails in a way we didn't foresee, does it fail safe, and can we explain it?* | Carries the final 0.4% |

The 50 questions you mapped live in the middle pillar. They are necessary and you should finish them. But the climb showed you the two pillars on either side — *define correct* above, *contain the untestable* below — and those are where the altitude actually is.

---

## The one question

If every question in this document collapsed into one — the question that contains all the others, the one worth writing above your desk:

> **When this fails in a way I did not foresee — and it will — does the system fail *safe*, and can I *explain why* afterward?**

Everything else is in service of those two clauses. *Fail safe* is containment. *Explain why* is reconstructability. A system that does both is trustworthy not because it never fails — nothing that touches a live market never fails — but because its failures are bounded and its history is legible.

You will never run out of questions. That is not the goal. The goal is a system where every failure is either **tested**, **contained**, or **consciously accepted with your eyes open** — and where the asking never stops, because the asking *is* the engineering.

That is where we land. Not at a finished list — there is no finished list — but at a discipline:

**Define what correct means. Test everything you can. Contain everything you can't. Make every failure legible. And never, ever stop asking the next question.**

---

*Keep climbing.*
