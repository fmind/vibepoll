// Phone voting client. The room answers at its own pace: this walks the deck
// one question at a time, advancing as each answer lands, and resumes where it
// left off after a reload because the server remembers what this voter answered.

const STORAGE_KEY = "vibepoll.voter";
const ADVANCE_MS = 260;

const el = (id) => document.getElementById(id);
const stages = {
  voting: el("stage-voting"),
  done: el("stage-done"),
  closed: el("stage-closed"),
  offline: el("stage-offline"),
};

/** A, B, C … then plain numbers past Z, for decks with many options. */
const optionKey = (index) => (index < 26 ? String.fromCharCode(65 + index) : String(index + 1));

// A random, opaque identifier minted in the browser. It is the only thing that
// ties a ballot to a device, it never leaves this origin, and it maps to no
// account, name, or address anywhere.
function voterId() {
  let id = null;
  try {
    id = localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private mode or blocked storage: fall through to a per-session id. The
    // voter simply starts over if they reload.
  }
  if (!id) {
    id = crypto.randomUUID().replaceAll("-", "");
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch { /* not fatal */ }
  }
  return id;
}

const VOTER = voterId();
let questions = [];
let state = null;
let cursor = null;      // index of the question on screen, or null to auto-pick
let pending = {};       // optimistic answers awaiting their broadcast echo

function show(name) {
  for (const [key, node] of Object.entries(stages)) {
    if (node) node.hidden = key !== name;
  }
}

function answers() {
  return { ...(state?.answers ?? {}), ...pending };
}

/** First unanswered question, or null when the deck is complete. */
function firstUnanswered() {
  const given = answers();
  const index = questions.findIndex((question) => !(question.id in given));
  return index === -1 ? null : index;
}

function renderPips() {
  const pips = el("pips");
  const given = answers();
  pips.hidden = questions.length === 0;
  pips.replaceChildren();
  questions.forEach((question, index) => {
    const pip = document.createElement("span");
    pip.className = "pip";
    if (question.id in given) pip.dataset.answered = "true";
    if (index === cursor) pip.dataset.current = "true";
    pips.append(pip);
  });
}

function renderQuestion(index) {
  const question = questions[index];
  if (!question) return;
  const chosen = answers()[question.id];

  el("question-title").textContent = question.title;
  el("question-subtitle").textContent = question.subtitle;

  const list = el("options");
  list.replaceChildren();
  question.options.forEach((option, position) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "option";
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(option.id === chosen));

    const key = document.createElement("span");
    key.className = "key";
    key.setAttribute("aria-hidden", "true");
    key.textContent = optionKey(position);

    const label = document.createElement("span");
    label.textContent = option.label;

    button.append(key, label);
    button.addEventListener("click", () => cast(question.id, option.id, index));
    item.append(button);
    list.append(item);
  });

  el("back").hidden = index === 0;
  // Forward only once this question is answered, so the deck cannot be skipped
  // on the way in but can be walked freely on the way back through.
  el("forward").hidden = !chosen || index === questions.length - 1;
  renderPips();
  show("voting");
}

function render() {
  if (!state || questions.length === 0) return;

  if (!state.votingOpen) {
    const finished = state.phase === "finished";
    el("closed-title").textContent = finished ? "That's a wrap." : "Look up.";
    el("closed-copy").textContent = finished
      ? "Final results are on the screen. Thanks for playing along."
      : "Voting is closed and the results are on the screen.";
    el("pips").hidden = true;
    show("closed");
    return;
  }

  const target = cursor ?? firstUnanswered();
  if (target === null) {
    el("pips").hidden = true;
    show("done");
    return;
  }
  cursor = target;
  renderQuestion(cursor);
}

async function cast(questionId, optionId, index) {
  // Show the choice immediately: the POST and its broadcast echo race, and a
  // button that un-highlights for a moment reads as a dropped tap.
  pending[questionId] = optionId;
  renderQuestion(index);

  setTimeout(() => {
    const next = index + 1;
    cursor = next < questions.length ? next : null;
    render();
  }, ADVANCE_MS);

  try {
    const response = await fetch("/api/vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: questionId, option: optionId, voter: VOTER }),
    });
    if (response.ok) return;
  } catch {
    // Nothing to retry against: a congested network is exactly when a retry
    // loop hurts most, and the next broadcast re-renders from server truth.
  }
  // Drop the optimistic copy and re-render, so the pip goes dark instead of
  // claiming an answer the server never accepted.
  delete pending[questionId];
  render();
}

el("back").addEventListener("click", () => {
  if (cursor !== null && cursor > 0) {
    cursor -= 1;
    render();
  }
});

el("forward").addEventListener("click", () => {
  if (cursor !== null && cursor < questions.length - 1) {
    cursor += 1;
    render();
  }
});

el("review-answers").addEventListener("click", () => {
  cursor = 0;
  render();
});

function connect() {
  const source = new EventSource(`/events?voter=${encodeURIComponent(VOTER)}`);
  source.addEventListener("state", (event) => {
    const next = JSON.parse(event.data);
    // Once the server confirms an answer, drop the optimistic copy so the two
    // cannot disagree after a vote is changed from another device.
    for (const [questionId, optionId] of Object.entries(next.answers ?? {})) {
      if (pending[questionId] === optionId) delete pending[questionId];
    }
    // Reopening voting after a review sends everyone back to their first gap.
    if (state && !state.votingOpen && next.votingOpen) cursor = null;
    state = next;
    render();
  });
  source.addEventListener("error", () => {
    if (source.readyState === EventSource.CLOSED) show("offline");
  });
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function start() {
  // Retry rather than give up: a single lost request during the scan rush would
  // otherwise leave this phone on an empty page until its owner reloaded.
  for (let attempt = 0; ; attempt += 1) {
    try {
      const response = await fetch("/api/poll");
      if (!response.ok) throw new Error(String(response.status));
      questions = (await response.json()).questions;
      render();
      connect();
      return;
    } catch {
      show("offline");
      await wait(Math.min(1000 * 2 ** attempt, 10000));
    }
  }
}

start();
