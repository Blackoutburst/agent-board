"use strict";

const Conversations = (() => {
  const targetCache = new Map();
  function targets(threadId) {
    if (!targetCache.has(threadId)) targetCache.set(threadId, api(`/threads/${encodeURIComponent(threadId)}/mentionables`).catch(error => { targetCache.delete(threadId); throw error; }));
    return targetCache.get(threadId);
  }
  function mentionEditor(textarea, draft, threadId) {
    const menu = el("div", { class: "mention-menu", id: "mention-options", role: "listbox", "aria-label": "Mention someone", hidden: true });
    const wrapper = el("div", { class: "mention-editor" }, textarea, menu);
    textarea.setAttribute("aria-controls", "mention-options"); textarea.setAttribute("aria-expanded", "false");
    let choices = [], selected = 0, generation = 0;
    const query = () => /(?:^|\s)@([^@\n]{0,100})$/.exec(textarea.value.slice(0, textarea.selectionStart));
    function hide() { menu.hidden = true; textarea.setAttribute("aria-expanded", "false"); }
    function choose(target) {
      const match = query(); if (!match) return;
      const end = textarea.selectionStart, start = end - match[1].length - 1;
      const text = `@${target.name} `;
      textarea.value = textarea.value.slice(0, start) + text + textarea.value.slice(end);
      draft.body = textarea.value;
      draft.mentionNames ||= {};
      draft.mentionNames[target.id] = target.name;
      draft.mentions = [...new Set([...draft.mentions, target.id])];
      hide(); textarea.focus(); textarea.setSelectionRange(start + text.length, start + text.length);
    }
    function render() {
      menu.replaceChildren(...choices.map((target, index) => btn("", () => choose(target), index === selected ? "selected" : "", { role: "option", "aria-selected": String(index === selected), onMousedown: event => event.preventDefault() })));
      [...menu.children].forEach((button, index) => button.append(el("strong", {}, `@${choices[index].name}`), el("span", { class: "small muted" }, `${choices[index].kind === "agent" ? "Agent" : "Human"} · ${ownerName(choices[index].owner_id)}`)));
      menu.hidden = !choices.length; textarea.setAttribute("aria-expanded", String(choices.length > 0));
    }
    async function update() {
      const own = ++generation, match = query();
      if (!match) { hide(); return; }
      const all = await targets(threadId);
      if (generation !== own || !textarea.isConnected) return;
      const filter = match[1].toLowerCase();
      choices = all.filter(item => item.name.toLowerCase().includes(filter)).slice(0, 12);
      selected = 0; render();
    }
    textarea.addEventListener("input", () => update().catch(() => hide()));
    textarea.addEventListener("click", () => update().catch(() => hide()));
    textarea.addEventListener("keydown", event => {
      if (menu.hidden) return;
      if (event.key === "Escape") { event.preventDefault(); ++generation; hide(); }
      else if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); selected = (selected + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length; render(); }
      else if (["Enter", "Tab"].includes(event.key)) { event.preventDefault(); ++generation; choose(choices[selected]); }
    });
    return wrapper;
  }
  function selectedMentions(draft) {
    return draft.mentions.filter(id => draft.mentionNames?.[id] && draft.body.includes(`@${draft.mentionNames[id]}`));
  }
  function messageBody(message) {
    return Markdown.block(message.body, "", message.mention_targets);
  }
  function attachment(item) {
    const url = safeUrl(item.url || `/api/attachments/${encodeURIComponent(item.id)}`);
    const preview = new URL(url); preview.searchParams.set("inline", "true");
    const link = el("a", { class: "attachment", href: url, download: "" }, `↓ ${item.filename} · ${bytes(item.size)}`);
    let media = null;
    if (["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"].includes(item.content_type)) media = el("img", { src: preview.href, alt: item.filename, loading: "lazy", class: "attachment-preview" });
    if (["video/mp4", "video/webm", "video/ogg"].includes(item.content_type)) media = el("video", { src: preview.href, controls: "", preload: "metadata", playsinline: "", class: "attachment-preview", "aria-label": item.filename });
    return el("div", { class: "attachment-item" }, media, link);
  }
  let running = false;
  async function refresh() {
    if (running || !state.actor || document.hidden) return;
    running = true;
    const request = state.request;
    try {
      const inbox = await api("/inbox");
      if (request !== state.request || !state.actor) return;
      const counter = $("inbox-count"); counter.hidden = !inbox.count; counter.textContent = String(inbox.count); counter.setAttribute("aria-label", `${inbox.count} inbox items`);
      if (state.view === "inbox") Workspace.renderInbox(inbox);
      if (["thread", "ticket"].includes(state.view) && state.threadId) {
        const threadId = state.threadId;
        const data = await api(`/threads/${encodeURIComponent(threadId)}?limit=50`);
        if (request !== state.request || state.threadId !== threadId) return;
        const changed = JSON.stringify(data.messages) !== JSON.stringify(state.threadData?.messages.slice(-50));
        if (changed) {
          const old = state.threadData?.messages || [];
          const seen = new Set(data.messages.map(message => message.id));
          state.threadData = { ...data, messages: [...old.filter(message => !seen.has(message.id) && message.created_at < (data.messages[0]?.created_at || 0)), ...data.messages] };
          const position = window.scrollY, bottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 100;
          renderMessages();
          if (bottom) $("messages").lastElementChild?.scrollIntoView({ block: "nearest" }); else window.scrollTo(0, position);
          await api(`/threads/${encodeURIComponent(threadId)}/read`, { method: "POST", data: {} });
        }
        if (state.view === "ticket" && !$("modal").open) {
          const info = await api(`/tickets/${encodeURIComponent(state.ticket.id)}`);
          if (request === state.request && info.ticket.version !== state.ticket.version) Workspace.refreshTicket(info);
        }
      }
      await loadThreads();
      await Workspace.refreshRequestCount();
    } catch (error) { if (error.status === 401) showLogin(); }
    finally { running = false; }
  }
  document.addEventListener("DOMContentLoaded", () => { refresh(); setInterval(refresh, 4000); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  return { mentionEditor, selectedMentions, messageBody, attachment, refresh };
})();
