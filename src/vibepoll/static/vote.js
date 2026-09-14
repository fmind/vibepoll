// One question at a time. Only confirmed answers count toward completion.
const el = (id) => document.getElementById(id);
const stages = {
  voting: el("stage-voting"),
  done: el("stage-done"),
  closed: el("stage-closed"),
  offline: el("stage-offline"),
};

function voterId() {
  let id;
  try {
    id = localStorage.getItem("vibepoll.voter");
  } catch { /* Blocked storage uses an id for this page session. */ }
  if (!id) {
    id = crypto.randomUUID().replaceAll("-", "");
    try {
      localStorage.setItem("vibepoll.voter", id);
    } catch { /* Reloading with blocked storage starts a new session. */ }
  }
  return id;
}

const VOTER = voterId();
let questions = [];
let state = null;
let cursor = null;
let saving = null;
let shownQuestion = null;
let shownStage = null;

function show(name) {
  for (const [key, node] of Object.entries(stages)) node.hidden = key !== name;
  el("progress").hidden = name !== "voting";
  if (shownStage !== name && name !== "voting") {
    stages[name].querySelector("h2").focus();
  }
  shownStage = name;
}

function firstUnanswered() {
  const index = questions.findIndex((question) => !state.answers[question.id]);
  return index === -1 ? null : index;
}

function renderQuestion(index) {
  const question = questions[index];
  const changed = shownQuestion !== question.id || shownStage !== "voting";
  if (changed) {
    el("question-title").textContent = question.title;
    el("question-subtitle").textContent = question.subtitle;
    el("vote-status").textContent = "";
    el("options").replaceChildren();
    question.options.forEach((option) => {
      const label = document.createElement("label");
      label.className = "option";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "answer";
      input.value = option.id;
      input.addEventListener("change", () => cast(question.id, option.id, index));
      const text = document.createElement("span");
      text.textContent = option.label;
      label.append(input, text);
      el("options").append(label);
    });
    shownQuestion = question.id;
  }

  const chosen = saving?.question === question.id ? saving.option : state.answers[question.id];
  for (const input of el("options").querySelectorAll("input")) {
    input.checked = input.value === chosen;
    input.disabled = saving !== null;
  }
  const answered = Boolean(state.answers[question.id]);
  el("back").hidden = index === 0;
  el("forward").hidden = !answered;
  el("forward").textContent = index === questions.length - 1 ? "Done" : "Next →";
  el("back").disabled = saving !== null;
  el("forward").disabled = saving !== null;
  el("progress").textContent = `Question ${index + 1} of ${questions.length}`;
  show("voting");
  if (changed) {
    el("question-title").focus();
    window.scrollTo(0, 0);
  }
}

function render() {
  if (!state || questions.length === 0) return;
  if (!state.votingOpen) {
    const finished = state.phase === "finished";
    el("closed-title").textContent = finished ? "That's a wrap." : "Look up.";
    el("closed-copy").textContent = finished
      ? "Thanks for sharing your experience. See you at the next session."
      : "Voting is closed and the results are on the screen.";
    show("closed");
    return;
  }
  cursor = cursor ?? firstUnanswered();
  if (cursor === null && saving === null) show("done");
  else renderQuestion(cursor ?? saving.index);
}

async function cast(question, option, index) {
  if (saving) return;
  saving = { question, option, index };
  renderQuestion(index);
  el("vote-status").textContent = "Saving…";
  try {
    const response = await fetch("/api/vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, option, voter: VOTER }),
      signal: AbortSignal.timeout(15000),
    });
    if (!response.ok) throw new Error("Vote not confirmed");
    state.answers[question] = option;
    // Keep the confirmed choice visible briefly and absorb a rapid second tap.
    await new Promise((resolve) => setTimeout(resolve, 260));
    saving = null;
    cursor = index + 1 < questions.length ? index + 1 : null;
    render();
  } catch {
    saving = null;
    render();
    el("vote-status").textContent = "Couldn't confirm your answer. Choose an answer to try again.";
    if (shownStage === "voting") el("vote-status").focus();
  }
}

el("back").addEventListener("click", () => {
  if (saving || cursor === null || cursor === 0) return;
  cursor -= 1;
  render();
});
el("forward").addEventListener("click", () => {
  if (saving || cursor === null) return;
  cursor = cursor + 1 < questions.length ? cursor + 1 : null;
  render();
});
el("review-answers").addEventListener("click", () => {
  cursor = 0;
  render();
});

function connect() {
  const source = new EventSource(`/events?voter=${encodeURIComponent(VOTER)}`);
  source.addEventListener("open", () => { el("connection-status").hidden = true; });
  source.addEventListener("state", (event) => {
    const next = JSON.parse(event.data);
    if (state && !state.votingOpen && next.votingOpen) cursor = null;
    state = next;
    el("connection-status").hidden = true;
    render();
  });
  source.addEventListener("error", () => {
    el("connection-status").hidden = false;
    if (!state) show("offline");
  });
}

async function start() {
  for (let attempt = 0; ; attempt += 1) {
    try {
      const response = await fetch("/api/poll");
      if (!response.ok) throw new Error("Poll unavailable");
      questions = (await response.json()).questions;
      connect();
      return;
    } catch {
      show("offline");
      await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * 2 ** attempt, 10000)));
    }
  }
}
start();
