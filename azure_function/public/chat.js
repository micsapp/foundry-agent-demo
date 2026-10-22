// Public chat page: sends each message to /api/chat/message and shows the agent's reply.
// The conversation and transcript live in sessionStorage, so a reload keeps the chat and a new tab starts fresh.
"use strict";
(() => {
  const STORAGE_KEY = "public-chat-v1";
  const TOOL_TEXT = {
    knowledge_base: "Searched the knowledge base",
    database: "Queried the database",
    feedback: "Logged for the team",
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
      if (saved && Array.isArray(saved.messages)) return saved;
    } catch (e) { /* storage blocked or corrupt: start fresh */ }
    return { conversationId: null, messages: [] };
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
    log.appendChild(el);
    scrollToEnd();
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
    if (busy || !text.trim()) return;
    busy = true;
    send.disabled = true;
    const question = { role: "user", text: text.trim() };
    state.messages.push(question);
    save();
    addMessage(question);
    const stopThinking = addThinking();
    try {
      const res = await fetch("/api/chat/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: question.text, conversation_id: state.conversationId }),
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
      const answer = { role: "assistant", text: data.reply, tools: data.tools || [] };
      state.messages.push(answer);
      save();
      addMessage(answer);
    } catch (e) {
      stopThinking();
      addNote("Couldn't reach the assistant. Check your connection and try again.", "error");
    } finally {
      busy = false;
      send.disabled = false;
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
    state = { conversationId: null, messages: [] };
    save();
    for (const el of [...log.children]) if (el !== intro) el.remove();
    intro.hidden = false;
    input.focus();
  });

  for (const msg of state.messages) addMessage(msg);
})();
