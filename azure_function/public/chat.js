// Public chat page: sends each message to /api/chat/message and shows the agent's reply.
// The conversation and transcript live in sessionStorage, so a reload keeps the chat and a new tab starts fresh.
"use strict";
(() => {
  const STORAGE_KEY = "public-chat-v2";
  const TOOL_TEXT = {
    knowledge_base: "Searched the knowledge base",
    database: "Queried the database",
    feedback: "Logged for the team",
    skills: "Loaded an agent skill",
    "incident report": "Generated an incident report",
    "incident update": "Ran the incident update tool",
    "knowledge base update": "Ran the knowledge base update tool",
  };
  const log = document.getElementById("log");
  const intro = document.getElementById("intro");
  const scroller = document.getElementById("scroller");
  const form = document.getElementById("composer");
  const input = document.getElementById("input");
  const send = document.getElementById("send");
  let state = load();
  let busy = false;

  if (window.DOMPurify) {
    DOMPurify.addHook("afterSanitizeAttributes", (node) => {
      if (node.tagName === "A") {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noopener noreferrer");
      }
    });
  }

  function load() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY));
      if (saved && Array.isArray(saved.messages) && /^[a-f0-9]{64}$/.test(saved.sessionToken)) return saved;
    } catch (e) { /* storage blocked or corrupt: start fresh */ }
    return freshState();
  }

  function freshState() {
    const bytes = crypto.getRandomValues(new Uint8Array(32));
    return { conversationId: null, messages: [], sessionToken: Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("") };
  }

  function save() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (e) { /* not essential */ }
  }

  function scrollToEnd() {
    scroller.scrollTop = scroller.scrollHeight;
  }

  function addMessage(msg) {
    intro.hidden = true;
    const el = document.createElement("div");
    el.className = "msg " + msg.role;
    const body = document.createElement("div");
    body.className = "body";
    if (msg.role === "assistant" && window.marked && window.DOMPurify) {
      body.innerHTML = DOMPurify.sanitize(marked.parse(msg.text, { gfm: true, breaks: true }));
    } else {
      body.textContent = msg.text; // user text, or the reply as plain text if the libraries didn't load
    }
    el.appendChild(body);
    if (msg.tools && msg.tools.length) {
      const tools = document.createElement("div");
      tools.className = "tools";
      for (const key of msg.tools) {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.textContent = TOOL_TEXT[key] || key;
        tools.appendChild(chip);
      }
      el.appendChild(tools);
    }
    if (msg.approvals && msg.approvals.length) addApprovals(msg, el);
    log.appendChild(el);
    scrollToEnd();
  }

  function addApprovals(msg, el) {
    msg.decisions = msg.decisions || {};
    const controls = [];
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "submit-decisions";
    submit.textContent = msg.submitted ? "Decisions submitted" : "Submit decisions";
    submit.dataset.approvalSubmit = "true";
    for (const action of msg.approvals) {
      const card = document.createElement("section");
      card.className = "approval";
      const title = document.createElement("h3");
      title.textContent = action.tool === "update_oracle_incident" ? "Proposed incident update" :
        action.tool === "propose_kb_file_update" ? "Proposed knowledge base update" : "Proposed action: " + action.tool;
      const content = document.createElement("pre");
      content.textContent = typeof action.arguments === "string" ? action.arguments : JSON.stringify(action.arguments, null, 2);
      card.append(title, content);
      const buttons = document.createElement("div");
      buttons.className = "approval-actions";
      for (const [label, approve] of [["Approve", true], ["Reject", false]]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.dataset.approvalButton = "true";
        button.addEventListener("click", () => {
          msg.decisions[action.id] = approve;
          save();
          refresh();
        });
        controls.push({ button, id: action.id, approve });
        buttons.appendChild(button);
      }
      card.appendChild(buttons);
      el.appendChild(card);
    }
    function refresh() {
      for (const control of controls) {
        control.button.setAttribute("aria-pressed", String(msg.decisions[control.id] === control.approve));
        control.button.disabled = busy || Boolean(msg.submitted);
      }
      submit.disabled = busy || Boolean(msg.submitted) || msg.approvals.some(a => typeof msg.decisions[a.id] !== "boolean");
      submit.textContent = msg.submitted ? "Decisions submitted" : "Submit decisions";
    }
    submit.addEventListener("click", () => request({
      approvals: msg.approvals.map(a => ({ approval_request_id: a.id, approve: msg.decisions[a.id] })),
    }, msg));
    el.appendChild(submit);
    msg.refreshApprovals = refresh;
    refresh();
  }

  function updateControls() {
    const pending = state.messages.some(msg => msg.approvals && msg.approvals.length && !msg.submitted);
    send.disabled = busy || pending;
    input.disabled = busy || pending;
    input.placeholder = pending ? "Review the proposed changes above first…" : "Ask a question…";
    document.getElementById("new-chat").disabled = busy;
    for (const msg of state.messages) if (msg.refreshApprovals) msg.refreshApprovals();
  }

  function addNote(text, kind) {
    const el = document.createElement("div");
    el.className = "note " + kind;
    el.textContent = text;
    log.appendChild(el);
    scrollToEnd();
  }

  function addThinking() {
    const el = document.createElement("div");
    el.className = "msg assistant thinking";
    el.innerHTML = '<div class="body">Thinking<span class="dots"><span>.</span><span>.</span><span>.</span></span></div>';
    log.appendChild(el);
    scrollToEnd();
    const slow = setTimeout(() => {
      el.firstChild.firstChild.textContent = "Still working, looking things up can take up to a minute";
    }, 12000);
    return () => { clearTimeout(slow); el.remove(); };
  }

  async function ask(text) {
    if (busy || input.disabled || !text.trim()) return;
    const question = { role: "user", text: text.trim() };
    state.messages.push(question);
    save();
    addMessage(question);
    await request({ message: question.text });
  }

  async function request(payload, approvalMessage) {
    if (busy) return;
    busy = true;
    updateControls();
    const stopThinking = addThinking();
    try {
      const res = await fetch("/api/chat/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, conversation_id: state.conversationId, session_token: state.sessionToken }),
      });
      let data = {};
      try { data = await res.json(); } catch (e) { /* not JSON */ }
      stopThinking();
      if (!res.ok) {
        addNote(data.error || "Something went wrong. Please try again.", "error");
        return;
      }
      if (data.new_conversation) addNote("Your earlier chat had expired, so this started a new one.", "info");
      state.conversationId = data.conversation_id;
      if (approvalMessage) {
        approvalMessage.submitted = true;
        approvalMessage.refreshApprovals();
      }
      const answer = { role: "assistant", text: data.reply, tools: data.tools || [], approvals: data.approvals || [] };
      state.messages.push(answer);
      save();
      addMessage(answer);
    } catch (e) {
      stopThinking();
      addNote("Couldn't reach the assistant. Check your connection and try again.", "error");
    } finally {
      busy = false;
      updateControls();
      input.focus();
    }
  }

  function resize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value;
    if (!text.trim() || busy) return;
    input.value = "";
    resize();
    ask(text);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener("input", resize);
  for (const button of intro.querySelectorAll("[data-q]")) {
    button.addEventListener("click", () => ask(button.dataset.q));
  }
  document.getElementById("new-chat").addEventListener("click", () => {
    if (busy) return;
    state = freshState();
    save();
    for (const el of [...log.children]) if (el !== intro) el.remove();
    intro.hidden = false;
    updateControls();
    input.focus();
  });

  for (const msg of state.messages) addMessage(msg);
  updateControls();
})();
