// Stage screen and presenter console.
//
// During the break the QR is pinned to the right third of the screen and only
// the left two thirds rotate: a welcome panel, then each question with its live
// results, then round again. Keeping the code fixed means nobody ever loses it
// mid-scan, which a full-screen carousel could not promise.
//
// The rotation is deliberately client-side. A server-driven one would broadcast
// a frame to every phone in the room every few seconds purely to move a panel
// nobody on a phone can see, and two screens would still drift on reconnect.
//
// When the presenter takes the stage the server's phase becomes `review`, the
// rotation stops, and the question fills the width with the QR gone.

const el = (id) => document.getElementById(id);
const screens = {
  ambient: el("screen-ambient"),
  question: el("screen-question"),
  finished: el("screen-finished"),
};
const panels = {
  welcome: el("panel-welcome"),
  question: el("panel-question"),
};

let panelMs = 5000;
let questions = [];
let state = null;
let panel = 0;   // 0 = welcome, 1..N = questions
let shown = null; // panel index currently on screen, so entrances play once
let timer = null;
let paused = false;

function show(name) {
  for (const [key, node] of Object.entries(screens)) {
    if (node) node.hidden = key !== name;
  }
}

function labelsFor(questionId) {
  const question = questions.find((item) => item.id === questionId);
  const labels = new Map();
  for (const option of question?.options ?? []) labels.set(option.id, option.label);
  return labels;
}

function renderBars(node, questionId, rows) {
  const labels = labelsFor(questionId);
  node.replaceChildren();
  rows.forEach((row) => {
    const item = document.createElement("li");
    item.className = "bar";
    item.dataset.winner = String(row.winner);
    // Set through CSSOM rather than a style attribute so the strict
    // `style-src 'self'` policy stays in force.
    item.style.setProperty("--pct", `${row.percent}%`);

    const label = document.createElement("span");
    label.className = "bar-label";
    label.append(document.createTextNode(labels.get(row.id) ?? row.id));

    const value = document.createElement("span");
    value.className = "bar-value";
    value.textContent = `${row.percent}% · ${row.count}`;

    item.append(label, value);
    node.append(item);
  });
}

/** Replay the entrance animation on whichever panel just became current.
 *
 * The Web Animations API rather than a CSS class toggle: re-triggering a CSS
 * animation needs a forced reflow between removing and re-adding the class,
 * which is easy to get subtly wrong and silently leaves the panel unanimated.
 */
function animate(node) {
  if (typeof node?.animate !== "function") return;
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  node.animate(
    [
      { opacity: 0, transform: "translateY(14px)" },
      { opacity: 1, transform: "none" },
    ],
    { duration: 420, easing: "cubic-bezier(0.22, 1, 0.36, 1)" },
  );
}

function showPanel(name) {
  for (const [key, node] of Object.entries(panels)) {
    if (node) node.hidden = key !== name;
  }
  // Only when the carousel actually moved. render() also runs on every incoming
  // vote, and replaying the entrance there makes the projector flicker in time
  // with the room tapping their phones.
  if (panel !== shown) {
    animate(panels[name]);
    shown = panel;
  }
}

function resultsFor(question) {
  return state.results?.[question.id] ?? { total: 0, rows: [] };
}

function renderAmbient() {
  show("ambient");
  if (panel === 0) {
    el("a-people").textContent = String(state.participants);
    showPanel("welcome");
    return;
  }
  const question = questions[panel - 1];
  if (!question) return;
  const results = resultsFor(question);
  el("a-position").textContent = `[${panel}/${questions.length}]`;
  el("a-title").textContent = question.title;
  el("a-count").textContent = String(results.total);
  renderBars(el("a-bars"), question.id, results.rows);
  showPanel("question");
}

function renderReview(index) {
  const question = questions[index];
  if (!question) return;
  const results = resultsFor(question);
  el("q-position").textContent = `[${index + 1}/${questions.length}]`;
  el("q-title").textContent = question.title;
  el("q-count").textContent = String(results.total);
  renderBars(el("q-bars"), question.id, results.rows);
  show("question");
}

function render() {
  if (!state || questions.length === 0) return;

  if (state.phase === "finished") {
    stopRotation();
    show("finished");
    return;
  }
  if (state.phase === "review") {
    stopRotation();
    renderReview(state.index);
    return;
  }
  renderAmbient();
  armRotation();
}

// -- rotation ---------------------------------------------------------------

function stopRotation() {
  clearTimeout(timer);
  timer = null;
}

/** Start the countdown only when none is already running.
 *
 * render() runs on every incoming vote, so restarting the timer here would
 * leave a busy room staring at one panel for the entire break: with a vote
 * arriving more often than `panelMs`, the carousel would never fire.
 */
function armRotation() {
  if (timer !== null || paused || state?.phase !== "slideshow") return;
  timer = setTimeout(() => {
    timer = null;
    panel = (panel + 1) % (questions.length + 1);
    render();
  }, panelMs);
}

/** Step the rotation by hand, which also pauses it. */
function step(delta) {
  paused = true;
  stopRotation();
  const count = questions.length + 1;
  panel = (panel + delta + count) % count;
  render();
}

/** Back to the top of the carousel, running. */
function restartCarousel() {
  stopRotation();
  panel = 0;
  shown = null;
  paused = false;
}

// -- console ----------------------------------------------------------------

async function control(action) {
  try {
    const response = await fetch("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ action }),
    });
    if (!response.ok) throw new Error(String(response.status));
  } catch {
    el("console-hint").textContent = "network hiccup — press again";
  }
}

for (const button of document.querySelectorAll("[data-step]")) {
  button.addEventListener("click", () => {
    const delta = Number(button.dataset.step);
    if (state?.phase === "review") control(delta > 0 ? "next" : "prev");
    else if (state?.phase === "slideshow") step(delta);
  });
}

for (const button of document.querySelectorAll("[data-action]")) {
  button.addEventListener("click", () => {
    const action = button.dataset.action;
    // The only irreversible control on the console, so it asks once.
    if (action === "clear" && !confirm("Delete every vote for every question?")) return;
    if (action === "slideshow") restartCarousel();
    control(action);
  });
}

document.addEventListener("keydown", (event) => {
  // Space and Enter activate focused controls normally; other presenter
  // shortcuts still work after a mouse click leaves a button focused.
  if (event.target.closest("input, select, textarea")) return;
  if (event.target.closest("button, a") && [" ", "Enter"].includes(event.key)) return;
  const reviewing = state?.phase === "review";
  switch (event.key) {
    case " ":
      event.preventDefault();
      // One key to walk the talk: advance the review, or pause and resume the
      // break rotation without hunting for a button in a dark room.
      if (reviewing) control("next");
      else {
        paused = !paused;
        if (paused) stopRotation();
        else armRotation();
      }
      break;
    case "ArrowRight":
      event.preventDefault();
      if (reviewing) control("next");
      else step(1);
      break;
    case "ArrowLeft":
      event.preventDefault();
      if (reviewing) control("prev");
      else step(-1);
      break;
    case "s": case "S":
      restartCarousel();
      control("slideshow");
      break;
    case "r": case "R": control("review"); break;
    case "f": case "F": control("finish"); break;
    default: break;
  }
});

// -- connection -------------------------------------------------------------

function setOffline(offline) {
  el("link").hidden = !offline;
}

function connect() {
  const source = new EventSource("/events");
  source.addEventListener("open", () => setOffline(false));
  // EventSource reconnects on its own; this only tells the presenter that the
  // numbers on the projector have stopped moving for a reason.
  source.addEventListener("error", () => setOffline(true));
  source.addEventListener("state", (event) => {
    setOffline(false);
    const next = JSON.parse(event.data);
    // Returning to the break always restarts the rotation from the welcome.
    if (state && state.phase !== "slideshow" && next.phase === "slideshow") restartCarousel();
    state = next;
    render();
  });
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function start() {
  // Retry rather than give up: a single lost request on venue Wi-Fi would
  // otherwise leave the projector blank until somebody reloaded it by hand.
  for (let attempt = 0; ; attempt += 1) {
    try {
      const response = await fetch("/api/poll");
      if (!response.ok) throw new Error(String(response.status));
      const poll = await response.json();
      panelMs = poll.panelSeconds * 1000;
      questions = poll.questions;
      setOffline(false);
      connect();
      return;
    } catch {
      setOffline(true);
      await wait(Math.min(1000 * 2 ** attempt, 10000));
    }
  }
}

start();
