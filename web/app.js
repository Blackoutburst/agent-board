"use strict";

// All shared content is inserted as text. Agent credentials stay in memory only.
const basePath = location.pathname.endsWith("/") ? location.pathname : location.pathname.replace(/[^/]*$/, "");
const state = { actor: null, csrf: "", owners: [], threads: [], view: "welcome", threadId: null, threadData: null,
  settings: null, inboxCursor: null, inboxMessages: [], inboxTasks: [], drafts: new Map(), request: 0,
  projects: [], project: null, ticket: null, projectQuery: "", includeArchived: false, ticketFilters: new Map(),
  workDrafts: new Map() };
const $ = (id) => document.getElementById(id);
const idempotencyKey = () => typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
const names = { blackoutburst: "Blackoutburst", thescreamer: "TheScreamer" };
const ownerName = (id) => state.owners.find((owner) => owner.id === id || owner.owner_id === id)?.display_name || names[id] || id || "Unknown owner";
const lines = (value) => [...new Set(value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean))];
const readable = (value) => Array.isArray(value) ? value.join("\n") : value && typeof value === "object" ? JSON.stringify(value, null, 2) : String(value || "");
const date = (value) => value ? new Date(Number(value) * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
const bytes = (value) => value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil((value || 0) / 1024)} KB`;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "checked" || key === "disabled" || key === "hidden" || key === "required" || key === "readOnly") node[key] = value;
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child !== undefined && child !== null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
const btn = (label, action, className = "", props = {}) => el("button", { type: "button", class: className, onClick: action, ...props }, label);
const empty = (text) => el("p", { class: "empty" }, text);
function field(label, name, { type = "text", value = "", hint, required = false, placeholder = "", rows = 3 } = {}) {
  const input = type === "textarea" ? el("textarea", { name, rows, required, placeholder, "aria-label": label }) : el("input", { name, type, required, placeholder, "aria-label": label });
  input.value = value;
  return el("label", {}, label, input, hint ? el("span", { class: "field-hint" }, hint) : null);
}
function select(label, name, options, value) {
  const input = el("select", { name, "aria-label": label }, options.map(([id, text]) => el("option", { value: id }, text)));
  if (value !== undefined) input.value = value;
  return el("label", {}, label, input);
}
const check = (label, name, checked = false, value = "yes") => el("label", { class: "check" }, el("input", { type: "checkbox", name, checked, value }), el("span", {}, label));

let noticeTimer;
function notify(message, isError = false) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  $("notice").className = `notice${isError ? " error" : ""}`;
  $("notice").hidden = false;
  noticeTimer = setTimeout(() => { $("notice").hidden = true; }, isError ? 10000 : 4500);
}
async function api(path, { method = "GET", data, form } = {}) {
  const headers = {};
  if (state.csrf && method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  if (data !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(`${basePath}api${path}`, { method, credentials: "same-origin", headers, body: form || (data !== undefined ? JSON.stringify(data) : undefined) });
  let result;
  try { result = await response.json(); } catch { result = {}; }
  if (!response.ok) {
    if (response.status === 401 && path !== "/login") showLogin();
    const error = new Error(typeof result.detail === "string" ? result.detail : result.detail ? readable(result.detail) : `Request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return result;
}
function safeUrl(value) {
  try {
    if (value?.startsWith("/api/")) value = basePath + value.slice(1);
    const url = new URL(value || "", location.origin + basePath);
    return url.origin === location.origin && ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch { return "#"; }
}
function submit(form, action) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const buttons = [...form.querySelectorAll('button[type="submit"]')];
    buttons.forEach((button) => { button.disabled = true; });
    try { await action(new FormData(form)); }
    catch (error) { notify(error.message || "Could not connect to the board.", true); }
    finally { buttons.forEach((button) => { button.disabled = false; }); }
  });
  return form;
}
function openModal(title, content) {
  $("modal-content").replaceChildren(el("div", { class: "modal-head" }, el("h2", {}, title), btn("×", closeModal, "quiet", { "aria-label": "Close dialog" })), el("div", { class: "modal-body" }, content));
  $("modal").showModal();
}
function closeModal() { $("modal").close(); $("modal-content").replaceChildren(); }
function formButtons(label) { return el("div", { class: "button-row" }, btn("Cancel", closeModal, "quiet"), el("button", { type: "submit", class: "primary" }, label)); }
function head(title, subtitle, ...actions) {
  return el("header", { class: "page-head" }, el("div", {}, el("h1", {}, title), el("p", {}, subtitle)), el("div", { class: "button-row" }, actions));
}
function setView(view) {
  state.view = view;
  document.body.dataset.view = view;
  document.querySelectorAll('.workspace-nav .nav-button').forEach(node => node.classList.remove('active'));
  $("projects-nav").classList.toggle("active", ["projects", "project", "ticket"].includes(view));
  $("inbox-nav").classList.toggle("active", view === "inbox");
  $("settings-nav").classList.toggle("active", view === "settings");
  renderThreadList();
}
function showLogin() {
  state.actor = null;
  state.csrf = "";
  state.inboxCursor = null;
  state.inboxMessages = [];
  state.inboxTasks = [];
  state.drafts.clear();
  state.workDrafts.clear();
  state.request++;
  if ($("modal").open) closeModal();
  $("app").hidden = true;
  if ($("setup-view")) $("setup-view").hidden = true;
  $("login-view").hidden = false;
}
async function authenticated(result) {
  state.actor = result.actor;
  state.csrf = result.csrf_token;
  state.owners = result.owners || [{ id: "blackoutburst", display_name: "Blackoutburst" }, { id: "thescreamer", display_name: "TheScreamer" }];
  $("account-name").textContent = ownerName(state.actor.owner_id);
  $("login-view").hidden = true;
  if ($("setup-view")) $("setup-view").hidden = true;
  $("app").hidden = false;
  $("guide-link").href = basePath + "?page=setup";
  Workspace.refreshRequestCount().catch(() => {});
  await loadThreads();
  await loadRoute();
}

async function loadThreads() {
  state.threads = await api(`/threads?q=${encodeURIComponent($("search").value.trim())}`);
  renderThreadList();
}
function threadSubject(thread) {
  return thread.ticket ? thread.message_preview || `Échange à propos du ticket #${thread.ticket.number}` : thread.title;
}
function renderThreadList() {
  $("thread-list").replaceChildren(...state.threads.map((thread) => btn("", () => openThread(thread.id).catch((error) => notify(error.message, true)), `thread-item${state.view === "thread" && String(thread.id) === String(state.threadId) ? " active" : ""}`)));
  [...$("thread-list").children].forEach((button, index) => {
    const thread = state.threads[index];
    button.append(el("div", { class: "thread-name" }, el("span", {}, threadSubject(thread)), thread.unread_count ? el("span", { class: "unread", "aria-label": `${thread.unread_count} unread messages` }, thread.unread_count) : null), ...(thread.ticket ? [el("div", { class: "thread-context", lang: "fr" }, `${thread.ticket.project_name} · Ticket #${thread.ticket.number}`)] : []), el("div", { class: "thread-time" }, date(thread.updated_at)));
  });
  if (!state.threads.length) $("thread-list").append(empty($("search").value ? "No matching conversations." : "Ask a question, request a resource, or discuss a decision."));
}
function route(threadId, taskId, push = true) {
  const url = new URL(location.href);
  url.search = "";
  if (threadId) url.searchParams.set("thread", threadId);
  if (taskId) url.searchParams.set("task", taskId);
  if (push) history.pushState({}, "", url);
}
function showWelcome() {
  state.request++;
  setView("welcome");
  $("main").replaceChildren(el("div", { class: "welcome" }, el("p", { class: "eyebrow" }, "UN CONTEXTE COMMUN, UN RESPONSABLE"), el("h1", {}, "Les projets, les échanges et le travail à suivre."), el("p", {}, "Les projets et tickets décrivent le travail à réaliser. Les conversations servent à poser une question, demander une ressource ou valider une décision."), el("div", { class: "button-row" }, btn("Ouvrir les projets", () => loadProjects().catch(workError), "primary"), btn("New conversation", newThread))));
}
function newThread() {
  const key = idempotencyKey();
  const form = el("form", {}, field("Conversation title", "title", { required: true, placeholder: "Can you provide the visual reference?" }), el("p", { class: "small muted" }, "Choose a specific question or request, then invite the people who can help. Track the work itself in a project ticket."), state.owners.map((owner) => check(owner.display_name, "participants", true, owner.id || owner.owner_id)), formButtons("Create conversation"));
  submit(form, async (data) => {
    const participants = [...new Set([state.actor.owner_id, ...data.getAll("participants")])];
    const thread = await api("/threads", { method: "POST", data: { title: data.get("title").trim(), participants, idempotency_key: key } });
    closeModal();
    await loadThreads();
    await openThread(thread.id);
  });
  openModal("New conversation", form);
}
async function openThread(threadId, push = true, taskId) {
  const request = ++state.request;
  state.threadId = threadId;
  setView("thread");
  if (push) route(threadId, taskId);
  $("main").replaceChildren(empty("Loading conversation…"));
  const result = await api(`/threads/${encodeURIComponent(threadId)}?limit=50`);
  if (request !== state.request) return;
  state.threadData = result;
  renderThread();
  await api(`/threads/${encodeURIComponent(threadId)}/read`, { method: "POST", data: {} });
  await loadThreads();
}
function renderThread() {
  const data = state.threadData;
  const thread = data.thread;
  const people = (thread.participants || []).map((owner) => ownerName(typeof owner === "string" ? owner : owner.owner_id || owner.id)).join(" · ");
  const tasks = data.tasks || [];
  $("main").replaceChildren(head(threadSubject(thread), people || "Shared conversation", btn("Refresh", () => openThread(state.threadId, false).catch((error) => notify(error.message, true)), "quiet")),
    el("div", { class: `thread-layout${tasks.length ? " has-tasks" : ""}` }, el("section", { class: "conversation", "aria-label": "Messages" }, el("div", { id: "older-messages" }), el("div", { id: "messages", class: "messages" }), composer()), tasks.length ? el("aside", { class: "task-pane", "aria-label": "Conversation tasks" }, el("div", { class: "task-pane-head" }, el("h2", {}, "Tasks"), el("span", {}, `${tasks.length} total`)), tasks.map(taskCard)) : null));
  if (thread.ticket) $("main").prepend(el("nav", { class: "work-breadcrumb", "aria-label": "Projet et ticket", lang: "fr" }, workLink(thread.ticket.project_name, { project: thread.ticket.project_id }), el("span", { "aria-hidden": "true" }, " / "), workLink(`Retour au ticket #${thread.ticket.number}`, { ticket: thread.ticket.id })));
  renderMessages();
  const params = new URLSearchParams(location.search);
  const taskId = params.get("task");
  if (taskId) document.getElementById(`task-${taskId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  const messageId = params.get("message");
  if (messageId) document.getElementById(`message-${messageId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
}
function authorLabel(author = {}) {
  if (author.kind === "system") return "Board";
  const name = author.display_name || ownerName(author.owner_id);
  return author.kind === "agent" ? author.agent_name || `${name} · Agent` : name;
}
function authorMeta(author = {}, createdAt) {
  return el("div", { class: "message-meta" }, el("strong", {}, authorLabel(author)), el("span", { class: "badge" }, author.kind === "agent" ? "AGENT" : "HUMAN"), author.device_label ? el("span", { class: "muted" }, author.device_label) : null, author.session_label ? el("span", { class: "muted" }, `· ${author.session_label}`) : null, el("time", { class: "message-time", title: date(createdAt) }, date(createdAt)));
}
function renderMessages() {
  const data = state.threadData;
  $("messages").replaceChildren(...(data.messages.length ? data.messages.map(messageCard) : [empty(data.thread.ticket ? "Posez une question, demandez une ressource ou une validation. Cet échange apparaîtra dans les conversations après le premier message." : "Ask a question, request a resource, or discuss a decision.")]));
  $("older-messages").replaceChildren();
  if (data.next_before) $("older-messages").append(btn("Load older messages", async (event) => {
    event.currentTarget.disabled = true;
    try {
      const earlier = await api(`/threads/${encodeURIComponent(state.threadId)}?limit=50&before=${encodeURIComponent(data.next_before)}`);
      const existing = new Set(data.messages.map((message) => message.id));
      data.messages = [...earlier.messages.filter((message) => !existing.has(message.id)), ...data.messages];
      data.next_before = earlier.next_before;
      renderMessages();
    } catch (error) { notify(error.message, true); event.currentTarget.disabled = false; }
  }, "quiet"));
}
function messageCard(message) {
  const author = message.author || {};
  const extraMentions = (message.mention_targets || []).filter(target => !message.body.toLowerCase().includes(`@${target.name.toLowerCase()}`));
  const reply = message.reply_to ? state.threadData.messages.find((item) => String(item.id) === String(message.reply_to)) : null;
  return el("article", { class: `message${new URLSearchParams(location.search).get("message") === String(message.id) ? " highlight" : ""}`, id: `message-${message.id}` }, el("div", { class: `avatar${author.kind === "agent" ? " agent" : ""}`, "aria-hidden": "true" }, author.kind === "agent" ? "CX" : authorLabel(author).slice(0, 2).toUpperCase()),
    el("div", { class: "message-content" }, authorMeta(author, message.created_at), message.reply_to ? el("div", { class: "reply-reference" }, reply ? `Reply to ${authorLabel(reply.author)}: ${reply.body.slice(0, 100)}` : `Reply to message ${message.reply_to}`) : null,
      el("p", { class: "message-body" }, Conversations.messageBody(message)),
      extraMentions.length ? el("p", { class: "mention-line" }, extraMentions.map(target => el("span", { class: "mention" }, `@${target.name}`))) : null,
      message.attachments?.length ? el("div", { class: "attachments" }, message.attachments.map(Conversations.attachment)) : null,
      el("div", { class: "message-actions" }, btn("Reply", () => { const draft = getDraft(); draft.reply = message; renderComposer(); $("message-body").focus(); }))));
}
function getDraft() {
  if (!state.drafts.has(String(state.threadId))) state.drafts.set(String(state.threadId), { body: "", reply: null, mentions: [], files: [], attachments: [], key: idempotencyKey() });
  return state.drafts.get(String(state.threadId));
}
function renderComposer() { $("compose").replaceWith(composer()); }
function composer() {
  const draft = getDraft();
  const body = el("textarea", { id: "message-body", name: "body", placeholder: "Ask a question, request a resource or validation, or reply…", "aria-label": "Message", onInput: (event) => { draft.body = event.target.value; } });
  body.value = draft.body;
  const previews = el("div", { class: "compose-files", "aria-live": "polite" });
  const renderPreviews = () => previews.replaceChildren(...draft.files.map((item, index) => filePreview(item, () => { draft.files.splice(index, 1); draft.attachments = []; renderPreviews(); })));
  const file = el("input", { class: "file-input", type: "file", name: "files", multiple: "", hidden: true, tabindex: "-1", onChange: (event) => { draft.files = [...draft.files, ...event.target.files]; draft.attachments = []; event.target.value = ""; renderPreviews(); } });
  const clip = el("button", { type: "button", class: "attach-button", title: "Attach files", "aria-label": "Attach files", onClick: () => file.click() }, paperclipIcon());
  const editor = Conversations.mentionEditor(body, draft, state.threadId);
  editor.append(clip, file);
  renderPreviews();
  const form = el("form", { id: "compose", class: "composer" }, draft.reply ? el("div", { class: "reply-banner" }, `Replying to ${authorLabel(draft.reply.author)}`, btn("Cancel reply", () => { draft.reply = null; renderComposer(); }, "quiet")) : null,
    editor, previews, el("div", { class: "compose-tools" }, el("div", { class: "small muted" }, "Any file · Up to 10 MB each · Type @ to mention a person or agent"), el("button", { type: "submit", class: "primary" }, "Send message")));
  submit(form, async () => {
    if (!draft.body.trim() && !draft.files.length) throw new Error("Write a message or attach a file first.");
    if (draft.files.some((item) => item.size > 10 * 1024 * 1024)) throw new Error("Each attachment must be 10 MB or smaller.");
    const threadId = state.threadId;
    for (let index = draft.attachments.length; index < draft.files.length; index++) {
      const formData = new FormData();
      formData.append("file", draft.files[index]);
      draft.attachments.push(await api(`/threads/${encodeURIComponent(threadId)}/attachments`, { method: "POST", form: formData }));
    }
    const payload = { body: draft.body.trim(), reply_to: draft.reply?.id || null, mentions: Conversations.selectedMentions(draft), attachment_ids: draft.attachments.map((item) => item.id) };
    const payloadText = JSON.stringify(payload);
    if (draft.sentPayload && draft.sentPayload !== payloadText) draft.key = idempotencyKey();
    draft.sentPayload = payloadText;
    await api(`/threads/${encodeURIComponent(threadId)}/messages`, { method: "POST", data: { ...payload, idempotency_key: draft.key } });
    state.drafts.delete(String(threadId));
    if (state.view === "ticket" && state.ticket) await openTicket(state.ticket.id, false);
    else await openThread(threadId, false);
  });
  return form;
}

function paperclipIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 16 16"); svg.setAttribute("width", "16"); svg.setAttribute("height", "16"); svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M10.5 4.5 5.4 9.6a1.5 1.5 0 0 0 2.1 2.1l5.3-5.3a3 3 0 0 0-4.2-4.2L3.2 7.6a4.5 4.5 0 0 0 6.4 6.4l4.4-4.4");
  path.setAttribute("fill", "none"); path.setAttribute("stroke", "currentColor"); path.setAttribute("stroke-width", "1.5"); path.setAttribute("stroke-linecap", "round"); path.setAttribute("stroke-linejoin", "round");
  svg.append(path);
  return svg;
}
function filePreview(item, remove) {
  let thumb;
  if (item.type.startsWith("image/")) {
    thumb = el("img", { class: "compose-file-thumb", alt: "", src: URL.createObjectURL(item) });
    thumb.addEventListener("load", () => URL.revokeObjectURL(thumb.src), { once: true });
  } else thumb = el("span", { class: "compose-file-thumb compose-file-ext", "aria-hidden": "true" }, (item.name.split(".").pop() || "file").slice(0, 4).toUpperCase());
  return el("div", { class: `compose-file${item.size > 10 * 1024 * 1024 ? " too-large" : ""}` }, thumb,
    el("div", { class: "compose-file-info" }, el("span", { class: "compose-file-name", title: item.name }, item.name), el("span", { class: "small muted" }, item.size > 10 * 1024 * 1024 ? `${bytes(item.size)} · over 10 MB` : bytes(item.size))),
    btn("×", remove, "quiet compose-file-remove", { "aria-label": `Remove ${item.name}`, title: "Remove" }));
}

function destinationLabel(task) {
  if (task.destination_label) return task.destination_label;
  if (task.destination_kind === "human") return ownerName(task.destination_id);
  if (task.destination_kind === "group") return `${ownerName(task.destination_id)}'s Codex`;
  return `${task.destination_kind}: ${task.destination_id}`;
}
function taskStatus(task) {
  const statuses = { in_progress: "In progress", ready_for_verification: "Ready for verification", complete: "Complete", recovery: "Recovery — check previous work", needs_input: "Waiting for input" };
  if (task.status === "pending" && task.destination_online === false && ["device", "session"].includes(task.destination_kind)) {
    return `Waiting for ${task.destination_kind === "device" ? "computer" : "Codex chat"}: ${destinationLabel(task)}`;
  }
  return statuses[task.status] || `Waiting on ${destinationLabel(task)}`;
}
function taskCard(task) {
  const ownHuman = task.destination_kind === "human" && task.destination_id === state.actor.owner_id;
  const ownClaim = ownHuman && task.claimant && (typeof task.claimant !== "object" || (!task.claimant.session_id || task.claimant.session_id === state.actor.session_id));
  const pending = ["pending", "recovery"].includes(task.status);
  const details = el("details", {}, el("summary", {}, "Instructions & evidence"), el("p", { class: "detail-text" }, task.instructions || "No additional instructions."));
  for (const [label, value] of [["Requirements", task.requirements], ["Required capabilities", task.required_capabilities], ["Prerequisite tasks", task.prerequisites], ["Action", task.action], ["Resource", task.resource], ["Outcome", task.outcome], ["Verification evidence", task.evidence]]) {
    if (readable(value)) details.append(el("strong", { class: "small" }, label), el("p", { class: "detail-text" }, readable(value)));
  }
  if (task.verified) details.append(el("p", { class: "small", style: "color:var(--accent)" }, "✓ Verified completion recorded"));
  if (task.claimant) details.append(el("p", { class: "small muted" }, `Claimed by ${typeof task.claimant === "object" ? authorLabel(task.claimant) : String(task.claimant)}`, task.lease_expires_at ? ` · Claim expires ${date(task.lease_expires_at)}` : ""));
  const notes = Array.isArray(task.progress) ? task.progress : [];
  if (notes.length) details.append(el("strong", { class: "small" }, "Progress"), notes.map((item) => el("div", { class: "progress-note" }, el("span", { class: "muted" }, `${date(item.timestamp || item.created_at)}${item.author ? ` · ${typeof item.author === "object" ? authorLabel(item.author) : item.author}` : ""}`), el("p", {}, item.note || item.progress || ""))));
  const actions = el("div", { class: "button-row" });
  if (ownHuman && pending) actions.append(btn(task.status === "recovery" ? "Check & take over" : "Take responsibility", () => claimTask(task), "primary"));
  if (ownClaim && !pending && task.status !== "complete") {
    actions.append(btn("Update task", () => updateTask(task), "primary"));
    actions.append(btn("Renew claim", async () => {
      try { await api(`/tasks/${encodeURIComponent(task.id)}/update`, { method: "POST", data: { claim_version: task.claim_version } }); await openThread(task.thread_id, false); notify("Your task claim was renewed."); }
      catch (error) { notify(error.message, true); }
    }, "quiet"));
  }
  if (state.view !== "thread" || String(task.thread_id) !== String(state.threadId)) actions.append(btn("Open conversation", () => openThread(task.thread_id, true, task.id).catch((error) => notify(error.message, true)), "quiet"));
  return el("article", { class: `task-card${String(new URLSearchParams(location.search).get("task")) === String(task.id) ? " highlight" : ""}`, id: `task-${task.id}` }, el("div", { class: `task-status ${task.status}` }, taskStatus(task)), el("h3", {}, task.title), el("p", { class: "task-destination" }, `${task.destination_kind === "human" ? "HUMAN" : "AGENT"} · ${destinationLabel(task)}`), details, actions);
}
async function claimTask(task) {
  if (task.status === "recovery") {
    const form = el("form", {}, el("p", { class: "help-note" }, "The previous claim expired. Check whether the external action already happened before repeating it. Progress is preserved below the task."), field("What did you check?", "recovery_evidence", { type: "textarea", required: true, placeholder: "Record the current state and whether any previous changes already took effect." }), formButtons("Take responsibility"));
    submit(form, async (data) => { await doClaim(task, data.get("recovery_evidence").trim()); closeModal(); });
    openModal("Check previous work", form);
  } else {
    try { await doClaim(task, ""); } catch (error) { notify(error.message, true); }
  }
}
async function doClaim(task, recoveryEvidence) {
  const result = await api(`/tasks/${encodeURIComponent(task.id)}/claim`, { method: "POST", data: { recovery_evidence: recoveryEvidence } });
  notify(result.claimed ? "You have taken responsibility for this task." : result.reason || "This task is already assigned. Its current status has been refreshed.");
  await openThread(task.thread_id, true, task.id);
}
function updateTask(task) {
  const status = select("Status", "status", [["in_progress", "In progress"], ["needs_input", "Waiting for input"], ["ready_for_verification", "Ready for verification"], ["complete", "Complete"]], task.status === "needs_input" || task.status === "ready_for_verification" ? task.status : "in_progress");
  const verification = el("div", { hidden: true }, el("p", { class: "help-note" }, "Complete means the agreed checks passed. Record the outcome and verification evidence. A reply or an attempted action is not a verified result."), check("I verified the agreed acceptance checks passed", "verified"));
  status.querySelector("select").addEventListener("change", (event) => { verification.hidden = event.target.value !== "complete"; });
  const form = el("form", {}, status, field("Progress update", "progress", { type: "textarea", placeholder: "What changed? Is anything blocked?" }), field("Outcome", "outcome", { type: "textarea", value: task.outcome || "", placeholder: "The result of this action." }), field("Verification evidence", "evidence", { type: "textarea", value: task.evidence || "", placeholder: "Checks performed, observed results, or a link to evidence." }), verification, formButtons("Save update"));
  submit(form, async (data) => {
    const payload = { claim_version: task.claim_version, status: data.get("status"), progress: data.get("progress").trim(), outcome: data.get("outcome").trim(), evidence: data.get("evidence").trim(), verified: data.has("verified") };
    if (payload.status === "complete" && (!payload.outcome || !payload.evidence || !payload.verified)) throw new Error("Record the outcome and evidence, then confirm the acceptance checks passed.");
    await api(`/tasks/${encodeURIComponent(task.id)}/update`, { method: "POST", data: payload });
    closeModal();
    await openThread(task.thread_id, false);
    notify("Task updated.");
  });
  openModal("Update task", form);
}

async function loadInbox() { return Workspace.inbox(); }
function sessionPresence(session) {
  if (typeof session.available === "boolean") return session.available;
  if (typeof session.online === "boolean") return session.online;
  const lastSeen = session.last_seen_at || session.last_seen || session.presence_at || 0;
  return Number(lastSeen) > Date.now() / 1000 - 300;
}
async function loadSettings() { return Workspace.settings(); }
function notificationCard(result) {
  const card = el("section", { class: "settings-card notification-card" }, el("h2", {}, "Discord alerts"));
  if (result.status === "rejected") {
    card.append(el("p", { class: "error" }, "Alert status is unavailable. Refresh to try again."));
    return card;
  }
  const info = result.value;
  card.append(el("span", { class: `badge notification-state${info.enabled ? " enabled" : ""}` }, info.enabled ? "CONFIGURED" : "NOT CONFIGURED"),
    el("p", {}, "Shared conversation alerts go to the private channel for Blackoutburst and TheScreamer, even when your computers are off."),
    el("p", { class: "notification-events" }, "Mentions · New tasks · Waiting for input · Ready for verification · Complete"),
    el("p", { class: "small muted" }, "Private conversations stay out of Discord. Unchanged status and routine progress do not send alerts."));
  const actions = el("div", { class: "button-row" });
  try {
    const channel = new URL(info.channel_url || `https://discord.com/channels/${info.guild_id}/${info.channel_id}`);
    if (channel.protocol === "https:" && channel.hostname === "discord.com" && /^\/channels\/\d+\/\d+$/.test(channel.pathname)) {
      actions.append(el("a", { class: "nav-button", href: channel.href, target: "_blank", rel: "noopener" }, "Open Discord channel ↗"));
    }
  } catch { /* No channel link until a valid destination is configured. */ }
  if (info.can_test) {
    actions.append(btn("Send test alert", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await api("/notifications/test", { method: "POST", data: {} });
        notify("Test alert requested. Check the Discord channel for delivery.");
        await loadSettings();
      } catch (error) { notify(error.message, true); }
      finally { button.disabled = false; }
    }, "", { disabled: !info.enabled }));
  }
  actions.append(el("a", { class: "nav-button", href: "https://discord.com/oauth2/authorize?client_id=1464251071852445823&scope=bot&permissions=3072&guild_id=1463519251615973429&disable_guild_select=true", target: "_blank", rel: "noopener" }, "Invite Brokie bot ↗"));
  card.append(actions, el("p", { class: "small muted notification-note" }, "If the bot cannot access the destination, invite Brokie and grant View Channel and Send Messages in the chosen private channel."), el("p", { class: "small muted notification-note" }, "Set the Discord channel's Notification Settings to All Messages. Muted channels remain muted."));
  const pending = Array.isArray(info.pending) ? info.pending.length : Number(info.pending) || 0;
  if (pending > 0) card.append(el("p", { class: "small muted" }, `${pending} alert${pending === 1 ? "" : "s"} waiting for delivery.`));
  if (info.last_error) card.append(el("p", { class: "small error", role: "status" }, `Last delivery error: ${readable(info.last_error)}`));
  return card;
}
function deviceCard(device) {
  const revoked = !!device.revoked || !!device.revoked_at;
  return el("div", { class: "device" }, el("div", { class: "device-head" }, el("strong", { class: "small" }, device.label), revoked ? el("span", { class: "badge" }, "REVOKED") : btn("Revoke", async () => {
    if (!confirm(`Revoke ${device.label}? Its agents will lose board access. Your account and other computers remain available.`)) return;
    try { await api(`/settings/devices/${encodeURIComponent(device.id || device.device_id)}`, { method: "DELETE" }); await loadSettings(); notify("Computer access revoked."); }
    catch (error) { notify(error.message, true); }
  }, "danger")), el("p", {}, readable(device.capabilities).replaceAll("\n", ", ") || "No capabilities registered"));
}
function showToken(device) {
  const token = el("input", { class: "token", readOnly: true, "aria-label": "One-time agent token" });
  token.value = device.token;
  const copy = btn("Copy credential", async () => {
    try { await navigator.clipboard.writeText(token.value); notify("Credential copied. Store it in your MCP connection's secret configuration."); }
    catch { token.select(); notify("Select and copy the credential from this field."); }
  }, "primary");
  openModal("Computer registered", el("div", {}, el("p", {}, `Credential for ${device.label}. Copy it now; the board only reveals it once.`), token, el("p", { class: "help-note" }, "Store this credential in the computer's MCP configuration. Do not post it in a conversation. Each Codex chat registers its own session when it checks its inbox."), el("div", { class: "button-row" }, copy, btn("Done", closeModal)), el("p", { class: "small", style: "margin-top:20px" }, el("a", { href: basePath + "?page=setup", target: "_blank", rel: "noopener" }, "Open the connection setup guide ↗"))));
}

// Project and ticket views. Shared values are plain text, never HTML.
const ticketStatuses = { todo: "À faire", in_progress: "En cours", blocked: "Bloqué", review: "À valider", done: "Terminé" };
const ticketPriorities = { low: "Basse", normal: "Normale", high: "Haute", urgent: "Urgente" };
const workError = (error) => notify(error.message || "Impossible de joindre le tableau.", true);
const workPath = (value) => encodeURIComponent(String(value));
const workOptions = (values) => Object.entries(values);
function workUrl(params = {}) {
  const url = new URL(location.href);
  url.search = "";
  url.hash = "";
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== null) url.searchParams.set(key, value); });
  return url;
}
function workRoute(params, push = true) { if (push) history.pushState({}, "", workUrl(params)); }
function workLink(label, params, className = "") {
  return el("a", { href: workUrl(params).href, class: className, onClick: (event) => {
    if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    workRoute(params);
    loadRoute().catch(workError);
  } }, label);
}
async function loadRoute() {
  const params = new URLSearchParams(location.search);
  if (params.get("ticket")) return openTicket(params.get("ticket"), false);
  if (params.get("project")) return openProject(params.get("project"), false);
  if (params.get("page") === "projects") return loadProjects(false);
  if (params.get("page") === "settings") return loadSettings();
  if (params.get("page") === "inbox") return loadInbox();
  if (params.get("page") === "agents") return Workspace.agents();
  if (params.get("page") === "requests") return Workspace.requests();
  if (params.get("page") === "setup") return Workspace.setup();
  if (params.get("thread")) return openThread(params.get("thread"), false);
  return loadProjects(false);
}
function workBack(project, ticket = null) {
  return el("nav", { class: "work-breadcrumb", "aria-label": "Breadcrumb" }, workLink("Projects", { page: "projects" }), project ? el("span", { "aria-hidden": "true" }, " / ") : null, project ? workLink(project.name, { project: project.id }) : null, ticket ? el("span", {}, ` / Task #${ticket.number}`) : null);
}
function workText(title, value, fallback = "Non renseigné.") {
  return el("section", { class: "work-section" }, el("h2", {}, title), el("p", { class: `work-text${value ? "" : " muted"}` }, value || fallback));
}
function repositoryLink(value) {
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && !url.username && !url.password) return el("a", { href: url.href, target: "_blank", rel: "noopener noreferrer", class: "work-repository" }, value, " ↗");
  } catch { /* Invalid or absent repository links are rendered as text. */ }
  return el("span", { class: "muted work-text" }, value || "No repository linked.");
}
function projectStats(project) {
  const counts = project.ticket_counts || {};
  return Object.entries(ticketStatuses).map(([status, label]) => el("span", { class: `ticket-stat status-${status}` }, el("strong", {}, Number(counts[status]) || 0), ` ${label.toLowerCase()}`));
}
async function loadProjects(push = true) { return Workspace.projects(push); }
function getTicketFilters(projectId) {
  if (!state.ticketFilters.has(projectId)) state.ticketFilters.set(projectId, { query: "", status: "", priority: "", assignee_owner_id: "" });
  return state.ticketFilters.get(projectId);
}
async function openProject(projectId, push = true) { return Workspace.project(projectId, push); }
function ticketRow(ticket) {
  return el("article", { class: "ticket-row" }, el("div", { class: "ticket-row-title" }, el("span", { class: "ticket-number" }, `#${ticket.number}`), el("h3", {}, workLink(ticket.title, { ticket: ticket.id }))),
    el("div", { class: "ticket-row-meta" }, ticketStatusBadge(ticket.status), el("span", { class: `ticket-priority priority-${ticket.priority}` }, `Priorité ${ticketPriorities[ticket.priority]?.toLowerCase() || ticket.priority}`),
      el("span", { class: "ticket-owner" }, ticket.assignee_owner_id ? ownerName(ticket.assignee_owner_id) : "Non attribué"), ticket.claimant ? el("span", { class: "badge" }, "Pris en charge") : null));
}
function ticketStatusBadge(status) { return el("span", { class: `badge ticket-status status-${status}` }, ticketStatuses[status] || status); }
async function openTicket(ticketId, push = true) { return Workspace.ticket(ticketId, push); }
function renderTicket(info) { return Workspace.renderTicket(info); }
async function claimTicket(ticket, button) {
  button.disabled = true;
  try {
    await api(`/tickets/${workPath(ticket.id)}/claim`, { method: "POST", data: { expected_version: ticket.version } });
    await openTicket(ticket.id, false);
    notify("Vous avez pris ce ticket en charge.");
  } catch (error) {
    if (error.status === 409) { await openTicket(ticket.id, false); notify("Ce ticket a changé ou possède déjà un responsable actif. Sa version actuelle est affichée.", true); }
    else workError(error);
  } finally { button.disabled = false; }
}
function releaseTicket(ticket, project) {
  const form = el("form", {}, el("p", {}, `Libérer la prise en charge de « ${ticket.title} » ? L’attribution et l’historique du ticket sont conservés.`), el("p", { class: "help-note" }, "Prévenez le responsable avant une reprise afin d’éviter deux interventions simultanées."), workFormButtons("Libérer"));
  workSubmit(form, async () => {
    await api(`/tickets/${workPath(ticket.id)}`, { method: "PATCH", data: { expected_version: ticket.version, release_claim: true } });
    closeModal(); await openTicket(ticket.id, false); notify("Prise en charge libérée.");
  }, async () => { closeModal(); await openTicket(ticket.id, false); notify("Le ticket a changé. Relisez sa version actuelle avant de libérer sa prise en charge.", true); });
  openModal("Libérer le ticket", form);
}
function workFormButtons(label) {
  return el("div", { class: "button-row" }, btn("Fermer", closeModal, "quiet"), el("button", { type: "submit", class: "primary" }, label));
}
function boundedField(label, name, options, maxLength) {
  const control = field(label, name, options);
  control.querySelector("input,textarea").maxLength = maxLength;
  return control;
}
function draftValues(form) {
  const data = new FormData(form);
  const result = Object.fromEntries(data.entries());
  if (form.elements.participants) result.participants = data.getAll("participants");
  if (form.elements.archived) result.archived = data.has("archived");
  if ("assignee_owner_id" in result) result.assignee_owner_id ||= null;
  return result;
}
function bindWorkDraft(form, draft) {
  const save = () => { draft.values = draftValues(form); };
  form.addEventListener("input", save);
  form.addEventListener("change", save);
  form.append(el("p", { class: "small muted draft-note" }, "Votre brouillon est conservé dans cet onglet tant qu’il reste ouvert."));
}
function workSubmit(form, action, onConflict) {
  const errors = el("div", { class: "work-form-error", role: "alert", hidden: true });
  form.prepend(errors);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (form.dataset.busy === "true" || form.dataset.conflict === "true") return;
    const values = draftValues(form);
    form.dataset.busy = "true";
    const controls = [...form.querySelectorAll("input,textarea,select")].map((control) => [control, control.disabled]);
    controls.forEach(([control]) => { control.disabled = true; });
    const buttons = [...form.querySelectorAll('button[type="submit"]')];
    buttons.forEach((button) => { button.disabled = true; });
    errors.hidden = true;
    try { await action(values); }
    catch (error) {
      if (!form.isConnected) return;
      errors.hidden = false;
      errors.replaceChildren(el("p", {}, error.status === 409 ? "Une autre modification a été enregistrée. Votre brouillon est conservé ; comparez les versions avant de continuer." : error.message || "Impossible de joindre le tableau. Votre brouillon est conservé."));
      if (error.status === 409 && onConflict) {
        form.dataset.conflict = "true";
        errors.append(btn("Comparer avec la version actuelle", async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try { await onConflict(errors); }
          catch (problem) { errors.append(el("p", {}, problem.message || "Chargement impossible. Réessayez.")); }
          finally { button.disabled = false; }
        }));
      }
    } finally {
      form.dataset.busy = "false";
      controls.forEach(([control, disabled]) => { control.disabled = disabled; });
      buttons.forEach((button) => { button.disabled = form.dataset.conflict === "true"; });
    }
  });
}
function changedValues(values, base, fields) {
  return Object.fromEntries(fields.filter((key) => JSON.stringify(values[key] ?? "") !== JSON.stringify(base[key] ?? "")).map((key) => [key, values[key]]));
}
async function compareDraft(errors, draft, fields, fetchCurrent, reopen) {
  const current = await fetchCurrent();
  const changed = changedValues(draft.values, draft.base, fields);
  const externalChanges = fields.filter((key) => JSON.stringify(current[key] ?? "") !== JSON.stringify(draft.base[key] ?? ""));
  const labels = { name: "Nom", description: "Description", context: "Contexte", repository_url: "Dépôt", environment: "Environnement", archived: "Archivage", title: "Titre", acceptance_criteria: "Critères de validation", priority: "Priorité", assignee_owner_id: "Responsable", status: "Statut", outcome: "Résultat", evidence: "Preuves" };
  errors.querySelector(".work-conflict-comparison")?.remove();
  const comparison = el("div", { class: "work-conflict-comparison" }, el("h3", {}, `Version actuelle : ${current.version}`),
    el("p", {}, "Champs modifiés depuis l’ouverture de votre formulaire :"),
    externalChanges.length ? externalChanges.map((key) => el("div", { class: "conflict-field" }, el("strong", {}, labels[key] || key), el("p", { class: "work-text" }, typeof current[key] === "boolean" ? (current[key] ? "Oui" : "Non") : String(current[key] ?? "Non renseigné")), Object.hasOwn(changed, key) ? el("p", { class: "small" }, "Ce champ contient aussi une modification dans votre brouillon. Vérifiez-le avant d’enregistrer.") : null)) : el("p", {}, "La prise en charge ou une autre métadonnée a changé."),
    btn("Réexaminer mon brouillon sur cette version", () => {
      draft.base = current;
      draft.values = { ...current, ...changed };
      reopen();
      notify("Brouillon repris sur la version actuelle. Vérifiez les champs avant d’enregistrer.");
    }));
  errors.append(comparison);
}
function projectEditor(project = null) { return Workspace.projectEditor(project); }
function ticketEditor(ticket = null, project = state.project) { return Workspace.ticketEditor(ticket, project); }
function showSetup() { return Workspace.setup(); }

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button");
  button.disabled = true;
  $("login-error").hidden = true;
  try {
    const data = new FormData(form);
    const result = await api("/login", { method: "POST", data: { username: data.get("username").trim(), password: data.get("password") } });
    form.elements.password.value = "";
    await authenticated(result);
  } catch (error) { $("login-error").textContent = error.message; $("login-error").hidden = false; }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", () => { location.assign("https://blackoutburst.com/account/?action=logout"); });
$("new-thread").addEventListener("click", newThread);
$("inbox-nav").addEventListener("click", () => loadInbox().catch((error) => notify(error.message, true)));
$("settings-nav").addEventListener("click", () => loadSettings().catch((error) => notify(error.message, true)));
$("projects-nav").addEventListener("click", () => loadProjects().catch(workError));
let searchTimer;
$("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadThreads().catch((error) => notify(error.message, true)), 250); });
$("search-form").addEventListener("submit", (event) => { event.preventDefault(); clearTimeout(searchTimer); loadThreads().catch((error) => notify(error.message, true)); });
$("modal").addEventListener("cancel", () => { $("modal-content").replaceChildren(); });
window.addEventListener("popstate", () => { if (state.actor) loadRoute().catch(workError); });
document.addEventListener("DOMContentLoaded", () => {
  if (new URLSearchParams(location.search).get("page") === "setup") showSetup();
  else api("/me").then(authenticated).catch((error) => { showLogin(); if (error.status !== 401) notify(error.message || "Could not connect to the board.", true); });
});
