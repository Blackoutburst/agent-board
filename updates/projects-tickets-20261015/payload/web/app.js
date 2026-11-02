"use strict";

// All shared content is inserted as text. Agent credentials stay in memory only.
const basePath = location.pathname.endsWith("/") ? location.pathname : location.pathname.replace(/[^/]*$/, "");
const state = { actor: null, csrf: "", owners: [], threads: [], view: "welcome", threadId: null, threadData: null,
  settings: null, inboxCursor: null, inboxMessages: [], inboxTasks: [], drafts: new Map(), request: 0,
  projects: [], project: null, ticket: null, projectQuery: "", includeArchived: false, ticketFilters: new Map(),
  workDrafts: new Map(), discussionContexts: new Map() };
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
  const input = type === "textarea" ? el("textarea", { name, rows, required, placeholder }) : el("input", { name, type, required, placeholder });
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
  state.discussionContexts.clear();
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
  await loadThreads();
  await loadRoute();
}

async function loadThreads() {
  state.threads = await api(`/threads?q=${encodeURIComponent($("search").value.trim())}`);
  renderThreadList();
}
function renderThreadList() {
  $("thread-list").replaceChildren(...state.threads.map((thread) => btn("", () => openThread(thread.id).catch((error) => notify(error.message, true)), `thread-item${state.view === "thread" && String(thread.id) === String(state.threadId) ? " active" : ""}`)));
  [...$("thread-list").children].forEach((button, index) => {
    const thread = state.threads[index];
    button.append(el("div", { class: "thread-name" }, el("span", {}, thread.title), thread.unread_count ? el("span", { class: "unread", "aria-label": `${thread.unread_count} unread messages` }, thread.unread_count) : null), el("div", { class: "thread-time" }, date(thread.updated_at)));
  });
  if (!state.threads.length) $("thread-list").append(empty($("search").value ? "No matching conversations." : "Start your first conversation."));
}
function route(threadId, taskId, push = true) {
  const url = new URL(location.href);
  url.search = "";
  if (threadId) url.searchParams.set("thread", threadId);
  if (taskId) url.searchParams.set("task", taskId);
  if (threadId && state.discussionContexts.has(String(threadId))) url.searchParams.set("from_ticket", state.discussionContexts.get(String(threadId)).ticket.id);
  if (push) history.pushState({}, "", url);
}
function showWelcome() {
  state.request++;
  setView("welcome");
  $("main").replaceChildren(el("div", { class: "welcome" }, el("p", { class: "eyebrow" }, "UN CONTEXTE COMMUN, UN RESPONSABLE"), el("h1", {}, "Les projets, les échanges et le travail à suivre."), el("p", {}, "Retrouvez le contexte d’un projet, ouvrez un ticket et échangez dans sa discussion. Vos conversations existantes restent accessibles."), el("div", { class: "button-row" }, btn("Ouvrir les projets", () => loadProjects().catch(workError), "primary"), btn("New conversation", newThread))));
}
function newThread() {
  const key = idempotencyKey();
  const form = el("form", {}, field("Conversation title", "title", { required: true, placeholder: "Games rework — OAuth and email setup" }), el("p", { class: "small muted" }, "Invite the people who should have access to this conversation."), state.owners.map((owner) => check(owner.display_name, "participants", true, owner.id || owner.owner_id)), formButtons("Create conversation"));
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
  $("main").replaceChildren(head(thread.title, people || "Shared conversation", btn("Refresh", () => openThread(state.threadId, false).catch((error) => notify(error.message, true)), "quiet"), btn("Create task", newTask, "primary")),
    el("div", { class: "thread-layout" }, el("section", { class: "conversation", "aria-label": "Messages" }, el("div", { id: "older-messages" }), el("div", { id: "messages", class: "messages" }), composer()), el("aside", { class: "task-pane", "aria-label": "Conversation tasks" }, el("div", { class: "task-pane-head" }, el("h2", {}, "Tasks"), el("span", {}, `${data.tasks.length} total`)), data.tasks.length ? data.tasks.map(taskCard) : empty("No tasks yet. Create one when an action needs a clear owner."))));
  const context = state.discussionContexts.get(String(thread.id));
  if (context) $("main").prepend(el("nav", { class: "work-breadcrumb", "aria-label": "Projet et ticket", lang: "fr" }, workLink(context.project.name, { project: context.project.id }), el("span", { "aria-hidden": "true" }, " / "), workLink(`Ticket #${context.ticket.number} · ${context.ticket.title}`, { ticket: context.ticket.id })));
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
  return author.kind === "agent" ? `${name}'s Codex` : name;
}
function authorMeta(author = {}, createdAt) {
  return el("div", { class: "message-meta" }, el("strong", {}, authorLabel(author)), el("span", { class: "badge" }, author.kind === "agent" ? "AGENT" : "HUMAN"), author.device_label ? el("span", { class: "muted" }, author.device_label) : null, author.session_label ? el("span", { class: "muted" }, `· ${author.session_label}`) : null, el("time", { class: "message-time", title: date(createdAt) }, date(createdAt)));
}
function renderMessages() {
  const data = state.threadData;
  $("messages").replaceChildren(...(data.messages.length ? data.messages.map(messageCard) : [empty("Start the conversation. What needs to happen next?")]));
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
  const reply = message.reply_to ? state.threadData.messages.find((item) => String(item.id) === String(message.reply_to)) : null;
  return el("article", { class: `message${new URLSearchParams(location.search).get("message") === String(message.id) ? " highlight" : ""}`, id: `message-${message.id}` }, el("div", { class: `avatar${author.kind === "agent" ? " agent" : ""}`, "aria-hidden": "true" }, author.kind === "agent" ? "CX" : authorLabel(author).slice(0, 2).toUpperCase()),
    el("div", { class: "message-content" }, authorMeta(author, message.created_at), message.reply_to ? el("div", { class: "reply-reference" }, reply ? `Reply to ${authorLabel(reply.author)}: ${reply.body.slice(0, 100)}` : `Reply to message ${message.reply_to}`) : null,
      el("p", { class: "message-body" }, message.body), message.mentions?.length ? el("p", { class: "mention-line" }, message.mentions.map((owner) => `@${ownerName(owner)}`).join(" ")) : null,
      message.attachments?.length ? el("div", { class: "attachments" }, message.attachments.map((attachment) => el("a", { class: "attachment", href: safeUrl(attachment.url || `/api/attachments/${encodeURIComponent(attachment.id)}`), download: "" }, `↓ ${attachment.filename} · ${bytes(attachment.size)}`))) : null,
      el("div", { class: "message-actions" }, btn("Reply", () => { const draft = getDraft(); draft.reply = message; renderComposer(); $("message-body").focus(); }))));
}
function getDraft() {
  if (!state.drafts.has(String(state.threadId))) state.drafts.set(String(state.threadId), { body: "", reply: null, mentions: [], files: [], attachments: [], key: idempotencyKey() });
  return state.drafts.get(String(state.threadId));
}
function renderComposer() { $("compose").replaceWith(composer()); }
function composer() {
  const draft = getDraft();
  const body = el("textarea", { id: "message-body", name: "body", placeholder: "Share an update, ask a question, or reply…", "aria-label": "Message", onInput: (event) => { draft.body = event.target.value; } });
  body.value = draft.body;
  const file = el("input", { class: "file-input", type: "file", name: "files", multiple: "", "aria-label": "Attach files", onChange: (event) => { draft.files = [...event.target.files]; draft.attachments = []; } });
  const members = state.threadData.thread.participants || state.owners.map((owner) => owner.id || owner.owner_id);
  const form = el("form", { id: "compose", class: "composer" }, draft.reply ? el("div", { class: "reply-banner" }, `Replying to ${authorLabel(draft.reply.author)}`, btn("Cancel reply", () => { draft.reply = null; renderComposer(); }, "quiet")) : null,
    body, el("div", { class: "mentions" }, el("span", { class: "muted" }, "Mention:"), members.map((owner) => {
      const id = typeof owner === "string" ? owner : owner.owner_id || owner.id;
      const checkbox = check(ownerName(id), "mentions", draft.mentions.includes(id), id);
      checkbox.querySelector("input").addEventListener("change", (event) => { draft.mentions = event.target.checked ? [...draft.mentions, id] : draft.mentions.filter((item) => item !== id); });
      return checkbox;
    })), el("div", { class: "compose-tools" }, el("div", {}, file, draft.files.length ? el("div", { class: "small muted" }, draft.files.map((item) => item.name).join(", ")) : el("div", { class: "small muted" }, "Screenshots & documents · Up to 10 MB each")), el("button", { type: "submit", class: "primary" }, "Send message")));
  submit(form, async () => {
    if (!draft.body.trim() && !draft.files.length) throw new Error("Write a message or attach a file first.");
    if (draft.files.some((item) => item.size > 10 * 1024 * 1024)) throw new Error("Each attachment must be 10 MB or smaller.");
    const threadId = state.threadId;
    for (let index = draft.attachments.length; index < draft.files.length; index++) {
      const formData = new FormData();
      formData.append("file", draft.files[index]);
      draft.attachments.push(await api(`/threads/${encodeURIComponent(threadId)}/attachments`, { method: "POST", form: formData }));
    }
    const payload = { body: draft.body.trim(), reply_to: draft.reply?.id || null, mentions: draft.mentions, attachment_ids: draft.attachments.map((item) => item.id) };
    const payloadText = JSON.stringify(payload);
    if (draft.sentPayload && draft.sentPayload !== payloadText) draft.key = idempotencyKey();
    draft.sentPayload = payloadText;
    await api(`/threads/${encodeURIComponent(threadId)}/messages`, { method: "POST", data: { ...payload, idempotency_key: draft.key } });
    state.drafts.delete(String(threadId));
    await openThread(threadId, false);
  });
  return form;
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
async function newTask() {
  try { state.settings = await api("/settings"); }
  catch (error) { notify(error.message, true); return; }
  const key = idempotencyKey();
  const owners = state.settings.owners || state.owners;
  const destinations = el("div");
  const kind = select("Who should handle this?", "destination_kind", [["human", "A human"], ["group", "An owner's Codex agents"], ["device", "A particular computer"], ["session", "A particular Codex chat"]]);
  const updateDestination = () => {
    const value = kind.querySelector("select").value;
    let choices;
    if (value === "human" || value === "group") choices = owners.map((owner) => [owner.id || owner.owner_id, owner.display_name || ownerName(owner.id || owner.owner_id)]);
    if (value === "device") choices = (state.settings.routing?.devices || state.settings.devices || []).filter((device) => !device.revoked && !device.revoked_at).map((device) => [device.id || device.device_id, `${ownerName(device.owner_id || state.actor.owner_id)} · ${device.label}`]);
    if (value === "session") choices = (state.settings.routing?.sessions || state.settings.sessions || []).filter((session) => !session.revoked && !session.revoked_at).map((session) => [session.id || session.session_id, `${ownerName(session.owner_id || state.actor.owner_id)} · ${session.label || session.session_label}${sessionPresence(session) ? " · available" : " · offline"}`]);
    destinations.replaceChildren(select("Destination", "destination_id", choices?.length ? choices : [["", "No registered destination available"]]));
  };
  kind.querySelector("select").addEventListener("change", updateDestination);
  updateDestination();
  const prerequisites = el("div", {}, el("p", { class: "small muted" }, "Optional: wait for these tasks to finish first."), state.threadData.tasks.map((task) => check(task.title, "prerequisites", false, task.id)));
  const scope = el("details", {}, el("summary", { class: "small muted" }, "Agent scope & dependencies"), el("p", { class: "help-note" }, "Every agent task needs an action and a resource that match the owner's allowlists exactly. Capabilities describe access; they do not grant permission."), field("Action", "action", { placeholder: "Exact action from the owner's policy" }), field("Resource", "resource", { placeholder: "Exact resource from the owner's policy" }), field("Required capabilities", "required_capabilities", { type: "textarea", hint: "One capability per line. These must be available on the receiving device and session." }), prerequisites);
  kind.querySelector("select").addEventListener("change", (event) => { scope.open = event.target.value !== "human"; });
  const form = el("form", {}, field("Task title", "title", { required: true, placeholder: "Add the email provider's DNS records" }), kind, destinations,
    field("Instructions", "instructions", { type: "textarea", required: true, placeholder: "Describe the requested action and the exact expected result." }),
    field("Requirements & acceptance checks", "requirements", { type: "textarea", placeholder: "Exact records, links, checks, or information needed to do this safely." }),
    scope, formButtons("Create task"));
  submit(form, async (data) => {
    if (!data.get("destination_id")) throw new Error("Register a device or session before assigning work to it.");
    if (data.get("destination_kind") !== "human" && (!data.get("action").trim() || !data.get("resource").trim())) {
      scope.open = true;
      (!data.get("action").trim() ? form.elements.action : form.elements.resource).focus();
      throw new Error("Agent tasks need an explicit allowed action and resource. Add both before creating the task.");
    }
    await api(`/threads/${encodeURIComponent(state.threadId)}/tasks`, { method: "POST", data: { title: data.get("title").trim(), instructions: data.get("instructions").trim(), requirements: data.get("requirements").trim(), destination_kind: data.get("destination_kind"), destination_id: data.get("destination_id"), action: data.get("action").trim(), resource: data.get("resource").trim(), required_capabilities: lines(data.get("required_capabilities")), prerequisites: data.getAll("prerequisites"), idempotency_key: key } });
    closeModal();
    await openThread(state.threadId, false);
    notify("Task created.");
  });
  openModal("Create a task", form);
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

async function loadInbox() {
  const request = ++state.request;
  setView("inbox");
  route(null);
  $("main").replaceChildren(empty("Checking your inbox…"));
  const result = await api(`/inbox${state.inboxCursor ? `?cursor=${encodeURIComponent(state.inboxCursor)}` : ""}`);
  if (request !== state.request) return;
  state.inboxCursor = result.cursor;
  const messages = new Map(state.inboxMessages.map((message) => [message.id, message]));
  result.messages.forEach((message) => messages.set(message.id, message));
  state.inboxMessages = [...messages.values()].sort((a, b) => b.created_at - a.created_at);
  state.inboxTasks = result.tasks;
  $("main").replaceChildren(head("Your inbox", "Shared updates, mentions, and tasks you can take responsibility for.", btn("Check again", () => loadInbox().catch((error) => notify(error.message, true)), "quiet")),
    el("div", { class: "page-body" }, el("section", { class: "inbox-section" }, el("h2", {}, "Tasks for you"), state.inboxTasks.length ? el("div", { class: "inbox-grid" }, state.inboxTasks.map(taskCard)) : empty("No tasks are waiting for you.")),
      el("section", { class: "inbox-section" }, el("h2", {}, "Shared messages"), state.inboxMessages.length ? state.inboxMessages.map((message) => {
        const button = btn("", () => openThread(message.thread_id).catch((error) => notify(error.message, true)), "inbox-message");
        button.append(authorMeta(message.author, message.created_at), el("p", {}, message.body), message.mentions?.includes(state.actor.owner_id) ? el("span", { class: "badge" }, "MENTIONED YOU") : null);
        return button;
      }) : empty("No shared updates yet. Mention someone in a conversation to draw their attention."))));
  await loadThreads();
}
function sessionPresence(session) {
  if (typeof session.available === "boolean") return session.available;
  if (typeof session.online === "boolean") return session.online;
  const lastSeen = session.last_seen_at || session.last_seen || session.presence_at || 0;
  return Number(lastSeen) > Date.now() / 1000 - 300;
}
async function loadSettings() {
  const request = ++state.request;
  setView("settings");
  route(null);
  $("main").replaceChildren(empty("Loading your agent settings…"));
  const [settingsResult, notificationsResult] = await Promise.allSettled([api("/settings"), api("/notifications")]);
  if (request !== state.request) return;
  if (settingsResult.status === "rejected") throw settingsResult.reason;
  const settings = settingsResult.value;
  state.settings = settings;
  const policy = settings.policy || {};
  const policyForm = el("form", {}, check("Enable my agents on this board", "enabled", !!policy.enabled), check("Allow reading shared conversations and projects", "allow_read", !!policy.allow_read), check("Allow replies in shared conversations", "allow_reply", !!policy.allow_reply), check("Allow handling requests and managing projects / tickets", "allow_requests", !!policy.allow_requests), el("p", { class: "help-note", lang: "fr" }, "Cette permission autorise aussi la création et la modification des projets ainsi que la prise en charge des tickets. Un agent ne peut modifier un ticket qu’après l’avoir pris en charge. Les actions sur vos serveurs restent soumises à leurs autorisations propres."), el("hr"),
    field("Allowed actions", "actions", { type: "textarea", value: readable(policy.actions), placeholder: "inspect_dns\nverify_application", hint: "One exact action per line. A task must use one of these values." }),
    field("Allowed resources", "resources", { type: "textarea", value: readable(policy.resources), placeholder: "games.blackoutburst.com", hint: "One exact resource per line. No wildcard or inherited access." }),
    el("p", { class: "help-note" }, "Only you can change your agents' authorization. Leave actions and resources empty to allow discussion without permitting task execution."), el("button", { type: "submit", class: "primary" }, "Save my authorization"));
  submit(policyForm, async (data) => {
    await api("/settings/policy", { method: "PUT", data: { enabled: data.has("enabled"), allow_read: data.has("allow_read"), allow_reply: data.has("allow_reply"), allow_requests: data.has("allow_requests"), actions: lines(data.get("actions")), resources: lines(data.get("resources")) } });
    notify("Your agent authorization was saved.");
  });
  const deviceForm = el("form", {}, field("Computer name", "label", { required: true, placeholder: "Mac · Server management" }), field("Available capabilities", "capabilities", { type: "textarea", placeholder: "inspect_public_dns\nrepository:games", hint: "One per line. Each chat can register a subset of this computer's capabilities." }), el("button", { type: "submit", class: "primary" }, "Register computer"));
  submit(deviceForm, async (data) => {
    const device = await api("/settings/devices", { method: "POST", data: { label: data.get("label").trim(), capabilities: lines(data.get("capabilities")) } });
    await loadSettings();
    showToken(device);
  });
  const devices = settings.devices || [];
  const sessions = settings.sessions || [];
  $("main").replaceChildren(head("Your Codex agents", `Authorization and computers belonging to ${ownerName(state.actor.owner_id)}.`, btn("Refresh", () => loadSettings().catch((error) => notify(error.message, true)), "quiet")),
    el("div", { class: "page-body settings-grid" }, el("section", { class: "settings-card" }, el("h2", {}, "What my agents may do"), el("p", {}, "Choose the scope your agents may use when responding to shared project requests."), policyForm),
      el("div", {}, notificationCard(notificationsResult), el("section", { class: "settings-card" }, el("h2", {}, "Connected computers"), el("p", {}, "Each computer gets a separate, revocable credential. It never grants access as another owner."), devices.length ? devices.map(deviceCard) : empty("No computers registered yet."), el("hr"), el("h3", {}, "Add a computer"), deviceForm),
        el("section", { class: "settings-card" }, el("h2", {}, "Registered Codex chats"), el("p", {}, "A chat appears available after explicitly checking its inbox. Presence expires after five minutes."), sessions.length ? sessions.map((session) => el("div", { class: "session" }, el("strong", {}, session.label || session.session_label || session.id), el("span", { class: "badge", style: "margin-left:8px" }, sessionPresence(session) ? "AVAILABLE" : "OFFLINE"), el("p", {}, `${session.device_label || devices.find((device) => device.id === session.device_id)?.label || "Computer"} · ${readable(session.capabilities).replaceAll("\n", ", ") || "No capabilities"}`))) : empty("Chats register when they connect and check their inbox.")))));
}
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
  if (params.get("thread")) {
    if (params.get("from_ticket")) {
      const info = await api(`/tickets/${workPath(params.get("from_ticket"))}`);
      if (String(info.ticket.thread_id) === params.get("thread")) state.discussionContexts.set(String(info.ticket.thread_id), info);
    }
    return openThread(params.get("thread"), false);
  }
  showWelcome();
}
function workBack(project, ticket = null) {
  return el("nav", { class: "work-breadcrumb", "aria-label": "Fil d’Ariane" }, workLink("Projets", { page: "projects" }), project ? el("span", { "aria-hidden": "true" }, " / ") : null, project ? workLink(project.name, { project: project.id }) : null, ticket ? el("span", {}, ` / Ticket #${ticket.number}`) : null);
}
function workText(title, value, fallback = "Non renseigné.") {
  return el("section", { class: "work-section" }, el("h2", {}, title), el("p", { class: `work-text${value ? "" : " muted"}` }, value || fallback));
}
function repositoryLink(value) {
  try {
    const url = new URL(value);
    if (url.protocol === "https:" && !url.username && !url.password) return el("a", { href: url.href, target: "_blank", rel: "noopener noreferrer", class: "work-repository" }, value, " ↗");
  } catch { /* Invalid or absent repository links are rendered as text. */ }
  return el("span", { class: "muted work-text" }, value || "Aucun dépôt renseigné.");
}
function projectStats(project) {
  const counts = project.ticket_counts || {};
  return Object.entries(ticketStatuses).map(([status, label]) => el("span", { class: `ticket-stat status-${status}` }, el("strong", {}, Number(counts[status]) || 0), ` ${label.toLowerCase()}`));
}
async function loadProjects(push = true) {
  const request = ++state.request;
  setView("projects");
  workRoute({ page: "projects" }, push);
  const search = field("Rechercher un projet", "query", { type: "search", value: state.projectQuery, placeholder: "Nom ou description" });
  const archived = check("Inclure les projets archivés", "archived", state.includeArchived);
  const form = el("form", { class: "work-filters project-filters" }, search, archived, el("button", { type: "submit" }, "Rechercher"));
  const results = el("div", { class: "project-grid", "aria-live": "polite" }, empty("Chargement des projets…"));
  $("main").replaceChildren(el("div", { lang: "fr" }, head("Projets", "Un contexte partagé et des tickets pour avancer ensemble.", btn("Créer un projet", () => projectEditor(), "primary")), el("div", { class: "page-body" }, form, results)));
  let filterRequest = 0;
  const refresh = async () => {
    const serial = ++filterRequest;
    results.setAttribute("aria-busy", "true");
    try {
      const params = new URLSearchParams({ query: state.projectQuery, include_archived: String(state.includeArchived) });
      const projects = await api(`/projects?${params}`);
      if (request !== state.request || serial !== filterRequest) return;
      state.projects = projects;
      results.replaceChildren(...(projects.length ? projects.map((project) => el("article", { class: "project-card" },
        el("div", { class: "work-card-heading" }, el("h2", {}, workLink(project.name, { project: project.id })), project.archived ? el("span", { class: "badge" }, "Archivé") : null),
        el("p", { class: "project-description" }, project.description || "Ajoutez une description pour présenter ce projet."),
        el("div", { class: "project-counts" }, projectStats(project)),
        el("p", { class: "small muted" }, (project.participants || []).map(ownerName).join(" · ")),
        el("div", { class: "work-card-foot" }, el("span", { class: "small muted" }, `Mis à jour ${date(project.updated_at)}`), workLink("Ouvrir →", { project: project.id })))) : [empty(state.projectQuery || state.includeArchived ? "Aucun projet ne correspond à cette recherche." : "Aucun projet pour le moment. Créez-en un pour partager le contexte et organiser les premiers tickets.")]));
    } finally { if (request === state.request && serial === filterRequest) results.removeAttribute("aria-busy"); }
  };
  submit(form, async (data) => { state.projectQuery = String(data.get("query")).trim(); state.includeArchived = data.has("archived"); await refresh(); });
  await refresh();
}
function getTicketFilters(projectId) {
  if (!state.ticketFilters.has(projectId)) state.ticketFilters.set(projectId, { query: "", status: "", priority: "", assignee_owner_id: "" });
  return state.ticketFilters.get(projectId);
}
async function openProject(projectId, push = true) {
  const request = ++state.request;
  setView("project");
  workRoute({ project: projectId }, push);
  $("main").replaceChildren(empty("Chargement du projet…"));
  const project = await api(`/projects/${workPath(projectId)}`);
  if (request !== state.request) return;
  state.project = project;
  const filter = getTicketFilters(project.id);
  const filterForm = el("form", { class: "work-filters ticket-filters" },
    field("Rechercher un ticket", "query", { type: "search", value: filter.query, placeholder: "Titre ou description" }),
    select("Statut", "status", [["", "Tous les statuts"], ...workOptions(ticketStatuses)], filter.status),
    select("Priorité", "priority", [["", "Toutes les priorités"], ...workOptions(ticketPriorities)], filter.priority),
    select("Responsable", "assignee_owner_id", [["", "Tous les responsables"], ...(project.participants || []).map((id) => [id, ownerName(id)])], filter.assignee_owner_id),
    el("button", { type: "submit" }, "Filtrer"));
  const ticketList = el("div", { class: "ticket-list", "aria-live": "polite" }, empty("Chargement des tickets…"));
  const ticketCount = el("span", { class: "muted small" });
  const shared = el("details", { class: "project-context", open: "" }, el("summary", {}, "Contexte partagé"),
    workText("Brief et décisions", project.context, "Précisez l’objectif, les décisions prises, les contraintes et la prochaine étape."),
    el("div", { class: "project-context-grid" }, workText("Environnement", project.environment), el("section", { class: "work-section" }, el("h2", {}, "Dépôt"), repositoryLink(project.repository_url), el("h2", { class: "work-members-title" }, "Participants"), el("p", { class: "work-text" }, (project.participants || []).map(ownerName).join(" · ")))));
  $("main").replaceChildren(el("div", { class: "project-view", lang: "fr" }, workBack(project), head(project.name, project.description || "Contexte et tickets du projet.", btn("Modifier le projet", () => projectEditor(project)), btn("Créer un ticket", () => ticketEditor(null, project), "primary", { disabled: project.archived })),
    el("div", { class: "page-body" }, project.archived ? el("p", { class: "work-callout" }, "Projet archivé. Restaurez-le depuis « Modifier le projet » pour créer de nouveaux tickets.") : null,
      el("div", { class: "project-counts project-counts-large", "aria-label": "Tickets par statut" }, projectStats(project)), shared,
      el("section", { class: "work-section" }, el("div", { class: "work-section-head" }, el("h2", {}, "Tickets"), ticketCount), filterForm, ticketList))));
  let filterRequest = 0;
  const refresh = async () => {
    const serial = ++filterRequest;
    ticketList.setAttribute("aria-busy", "true");
    try {
      const tickets = await api(`/projects/${workPath(project.id)}/tickets?${new URLSearchParams(filter)}`);
      if (request !== state.request || serial !== filterRequest) return;
      ticketCount.textContent = `${tickets.length} affiché${tickets.length > 1 ? "s" : ""}`;
      ticketList.replaceChildren(...(tickets.length ? tickets.map(ticketRow) : [empty(Object.values(filter).some(Boolean) ? "Aucun ticket ne correspond aux filtres." : "Aucun ticket pour le moment. Décrivez le premier résultat attendu avec « Créer un ticket ».")]));
    } finally { if (request === state.request && serial === filterRequest) ticketList.removeAttribute("aria-busy"); }
  };
  submit(filterForm, async (data) => { Object.keys(filter).forEach((key) => { filter[key] = String(data.get(key) || "").trim(); }); await refresh(); });
  await refresh();
}
function ticketRow(ticket) {
  return el("article", { class: "ticket-row" }, el("div", { class: "ticket-row-title" }, el("span", { class: "ticket-number" }, `#${ticket.number}`), el("h3", {}, workLink(ticket.title, { ticket: ticket.id }))),
    el("div", { class: "ticket-row-meta" }, ticketStatusBadge(ticket.status), el("span", { class: `ticket-priority priority-${ticket.priority}` }, `Priorité ${ticketPriorities[ticket.priority]?.toLowerCase() || ticket.priority}`),
      el("span", { class: "ticket-owner" }, ticket.assignee_owner_id ? ownerName(ticket.assignee_owner_id) : "Non attribué"), ticket.claimant ? el("span", { class: "badge" }, "Pris en charge") : null));
}
function ticketStatusBadge(status) { return el("span", { class: `badge ticket-status status-${status}` }, ticketStatuses[status] || status); }
async function openTicket(ticketId, push = true) {
  const request = ++state.request;
  setView("ticket");
  workRoute({ ticket: ticketId }, push);
  $("main").replaceChildren(empty("Chargement du ticket…"));
  const info = await api(`/tickets/${workPath(ticketId)}`);
  if (request !== state.request) return;
  state.ticket = info.ticket;
  state.project = info.project;
  state.discussionContexts.set(String(info.ticket.thread_id), info);
  renderTicket(info);
}
function renderTicket({ ticket, project }) {
  const actions = [btn("Modifier", () => ticketEditor(ticket, project)), btn("Ouvrir la discussion →", () => openThread(ticket.thread_id).catch(workError), "primary")];
  const responsible = el("section", { class: "work-section ticket-responsibility" }, el("h2", {}, "Responsabilité"),
    el("p", {}, ticket.assignee_owner_id ? `Attribué à ${ownerName(ticket.assignee_owner_id)}.` : "Ce ticket n’est pas encore attribué."));
  if (ticket.claimant) {
    responsible.append(el("p", { class: "small" }, `Pris en charge par ${authorLabel(ticket.claimant)}.`, ticket.claimant.session_label ? ` Session : ${ticket.claimant.session_label}.` : "", ticket.claimant.device_label ? ` Appareil : ${ticket.claimant.device_label}.` : ""),
      btn("Libérer la prise en charge", () => releaseTicket(ticket, project), "quiet"));
  } else if (ticket.status !== "done" && (!ticket.assignee_owner_id || ticket.assignee_owner_id === state.actor.owner_id)) {
    responsible.append(btn("Prendre en charge", (event) => claimTicket(ticket, event.currentTarget), ""));
  } else if (ticket.status !== "done") {
    responsible.append(el("p", { class: "small muted" }, "Le responsable peut prendre ce ticket en charge. Modifiez l’attribution si une autre personne reprend le travail."));
  }
  responsible.append(el("p", { class: "small muted" }, "Une seule prise en charge active. L’attribution d’un ticket ne déclenche aucune action sur les serveurs."));
  $("main").replaceChildren(el("div", { class: "ticket-view", lang: "fr" }, workBack(project, ticket), head(`#${ticket.number} · ${ticket.title}`, project.name, ...actions),
    el("div", { class: "page-body" }, el("div", { class: "ticket-overview" }, ticketStatusBadge(ticket.status), el("span", { class: `ticket-priority priority-${ticket.priority}` }, `Priorité ${ticketPriorities[ticket.priority]?.toLowerCase() || ticket.priority}`), el("span", { class: "small muted" }, `Mis à jour ${date(ticket.updated_at)}`)),
      el("div", { class: "ticket-detail-grid" }, el("div", {}, workText("Description", ticket.description, "Précisez le problème à résoudre et le résultat attendu."), workText("Critères de validation", ticket.acceptance_criteria, "Définissez les vérifications qui permettront de valider ce ticket."),
        (ticket.outcome || ticket.evidence || ticket.status === "done") ? el("section", { class: "ticket-resolution" }, workText("Résultat", ticket.outcome), workText("Preuves de validation", ticket.evidence)) : null,
        el("section", { class: "work-section ticket-discussion" }, el("h2", {}, "Discussion et pièces jointes"), el("p", { class: "muted" }, "Les échanges, questions et fichiers de ce ticket sont regroupés dans une conversation dédiée, accessible aux participants du projet."), workLink("Ouvrir la discussion →", { thread: ticket.thread_id, from_ticket: ticket.id }, "nav-button"))), responsible),
      el("div", { class: "work-card-foot" }, workLink("← Retour au projet", { project: project.id }), btn("Actualiser", () => openTicket(ticket.id, false).catch(workError), "quiet")))));
}
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
function projectEditor(project = null) {
  const draftId = project ? `project:${project.id}` : "project:new";
  if (!state.workDrafts.has(draftId)) state.workDrafts.set(draftId, { key: idempotencyKey(), base: project || {}, values: project || { name: "", description: "", context: "", repository_url: "", environment: "", participants: state.owners.map((owner) => owner.id || owner.owner_id) } });
  const draft = state.workDrafts.get(draftId);
  const values = draft.values;
  const fields = ["name", "description", "context", "repository_url", "environment", "archived"];
  const participants = el("fieldset", { class: "work-participants" }, el("legend", {}, "Participants"), el("p", { class: "small muted" }, "Les participants auront accès au contexte et à tous les tickets du projet. Cette liste est fixée lors de la création."), state.owners.map((owner) => {
    const id = owner.id || owner.owner_id;
    const checkbox = check(owner.display_name || ownerName(id), "participants", (values.participants || []).includes(id), id);
    if (id === state.actor.owner_id) { checkbox.querySelector("input").checked = true; checkbox.querySelector("input").disabled = true; }
    return checkbox;
  }));
  const form = el("form", { class: "work-form", lang: "fr" },
    boundedField("Nom du projet", "name", { required: true, value: values.name, placeholder: "Plateforme Blackoutburst" }, 120),
    boundedField("Description", "description", { type: "textarea", value: values.description, rows: 2, placeholder: "Le périmètre du projet en quelques lignes." }, 4000),
    boundedField("Contexte partagé", "context", { type: "textarea", value: values.context, rows: 7, placeholder: "Objectif, état actuel, décisions, contraintes et prochaine étape.", hint: "Partagez uniquement les informations utiles aux participants. Aucun mot de passe ni secret." }, 30000),
    boundedField("Dépôt HTTPS", "repository_url", { type: "url", value: values.repository_url, placeholder: "https://github.com/organisation/projet" }, 2000),
    boundedField("Environnement", "environment", { type: "textarea", value: values.environment, placeholder: "Applications, branches et environnements concernés, sans identifiants secrets." }, 4000),
    project ? el("p", { class: "small muted" }, `Participants : ${(project.participants || []).map(ownerName).join(" · ")}`) : participants,
    project ? check("Archiver ce projet", "archived", !!values.archived) : null,
    workFormButtons(project ? "Enregistrer le projet" : "Créer le projet"));
  bindWorkDraft(form, draft);
  workSubmit(form, async (data) => {
    draft.values = data;
    if (!data.name.trim()) throw new Error("Donnez un nom au projet.");
    if (data.repository_url) {
      const url = new URL(data.repository_url);
      if (url.protocol !== "https:" || url.username || url.password) throw new Error("Le dépôt doit utiliser une URL HTTPS sans identifiant ni mot de passe.");
    }
    const payload = project ? { expected_version: draft.base.version, ...changedValues(data, draft.base, fields) } : { ...data, participants: [...new Set([state.actor.owner_id, ...(data.participants || [])])] };
    if (!project) {
      const sent = JSON.stringify(payload);
      if (draft.sentPayload && draft.sentPayload !== sent) draft.key = idempotencyKey();
      draft.sentPayload = sent;
      payload.idempotency_key = draft.key;
    }
    const saved = await api(project ? `/projects/${workPath(project.id)}` : "/projects", { method: project ? "PATCH" : "POST", data: payload });
    state.workDrafts.delete(draftId);
    closeModal();
    await openProject(saved.id);
    notify(project ? "Projet mis à jour." : "Projet créé.");
  }, project ? (errors) => compareDraft(errors, draft, fields, () => api(`/projects/${workPath(project.id)}`), () => projectEditor(project)) : null);
  openModal(project ? "Modifier le projet" : "Créer un projet", form);
}
function ticketEditor(ticket, project) {
  const draftId = ticket ? `ticket:${ticket.id}` : `ticket:new:${project.id}`;
  if (!state.workDrafts.has(draftId)) state.workDrafts.set(draftId, { key: idempotencyKey(), base: ticket || {}, values: ticket || { title: "", description: "", acceptance_criteria: "", priority: "normal", assignee_owner_id: null } });
  const draft = state.workDrafts.get(draftId);
  const values = draft.values;
  const fields = ["title", "description", "acceptance_criteria", "priority", "assignee_owner_id", "status", "outcome", "evidence"];
  const status = ticket ? select("Statut", "status", workOptions(ticketStatuses), values.status) : null;
  const outcome = ticket ? boundedField("Résultat", "outcome", { type: "textarea", value: values.outcome, rows: 3, placeholder: "Ce qui a été livré ou corrigé." }, 20000) : null;
  const evidence = ticket ? boundedField("Preuves de validation", "evidence", { type: "textarea", value: values.evidence, rows: 3, placeholder: "Tests effectués, résultats observés et liens vers les preuves." }, 20000) : null;
  const completionNote = el("p", { class: "help-note" }, "Pour terminer le ticket, indiquez le résultat et les preuves de validation.");
  const form = el("form", { class: "work-form", lang: "fr" }, el("p", { class: "small muted" }, project.name),
    boundedField("Titre du ticket", "title", { required: true, value: values.title, placeholder: "Activer la connexion Google" }, 200),
    boundedField("Description", "description", { type: "textarea", value: values.description, rows: 5, placeholder: "Le problème, le résultat attendu et les informations nécessaires." }, 20000),
    boundedField("Critères de validation", "acceptance_criteria", { type: "textarea", value: values.acceptance_criteria, rows: 4, placeholder: "Comment vérifier que le ticket est terminé ?" }, 20000),
    el("div", { class: "form-row" }, select("Priorité", "priority", workOptions(ticketPriorities), values.priority), select("Responsable", "assignee_owner_id", [["", "Non attribué"], ...(project.participants || []).map((id) => [id, ownerName(id)])], values.assignee_owner_id || "")),
    ticket?.claimant ? el("p", { class: "help-note" }, "Changer de responsable libère la prise en charge actuelle. Coordonnez la reprise avec le responsable avant de réattribuer le ticket.") : null,
    status, ticket ? el("section", { class: "work-resolution-form" }, el("h3", {}, "Résolution et validation"), completionNote, outcome, evidence) : null,
    workFormButtons(ticket ? "Enregistrer le ticket" : "Créer le ticket"));
  const validation = () => {
    if (!ticket) return;
    const done = status.querySelector("select").value === "done";
    outcome.querySelector("textarea").required = done;
    evidence.querySelector("textarea").required = done;
  };
  if (status) {
    status.querySelector("select").addEventListener("change", () => {
      if (draft.base.status === "done" && status.querySelector("select").value !== "done") completionNote.textContent = "À la réouverture, les anciens résultats et preuves inchangés sont effacés. Saisissez de nouvelles valeurs pour les conserver explicitement.";
      else completionNote.textContent = "Pour terminer le ticket, indiquez le résultat et les preuves de validation.";
      validation();
    });
    validation();
  }
  bindWorkDraft(form, draft);
  workSubmit(form, async (data) => {
    draft.values = data;
    if (!data.title.trim()) throw new Error("Donnez un titre au ticket.");
    if (data.status === "done" && (!data.outcome.trim() || !data.evidence.trim())) throw new Error("Renseignez le résultat et les preuves avant de terminer le ticket.");
    const payload = ticket ? { expected_version: draft.base.version, ...changedValues(data, draft.base, fields) } : data;
    if (!ticket) {
      const sent = JSON.stringify(payload);
      if (draft.sentPayload && draft.sentPayload !== sent) draft.key = idempotencyKey();
      draft.sentPayload = sent;
      payload.idempotency_key = draft.key;
    }
    const saved = await api(ticket ? `/tickets/${workPath(ticket.id)}` : `/projects/${workPath(project.id)}/tickets`, { method: ticket ? "PATCH" : "POST", data: payload });
    state.workDrafts.delete(draftId);
    closeModal();
    await openTicket(saved.id);
    await loadThreads();
    notify(ticket ? "Ticket mis à jour." : "Ticket créé avec sa discussion.");
  }, ticket ? (errors) => compareDraft(errors, draft, fields, async () => (await api(`/tickets/${workPath(ticket.id)}`)).ticket, () => ticketEditor(ticket, project)) : null);
  openModal(ticket ? `Modifier le ticket #${ticket.number}` : "Créer un ticket", form);
}

function showSetup() {
  document.title = "Codex setup · Shared board";
  $("app").hidden = true;
  $("login-view").hidden = true;
  $("setup-view").hidden = false;
  if (typeof crypto.randomUUID === "function") {
    $("guide-read-inbox").textContent = $("guide-read-inbox").textContent.replace("a-fresh-uuid-for-this-chat", crypto.randomUUID());
    $("guide-session-note").textContent = "This example has a fresh chat ID. Keep it for subsequent calls in this chat; choose a new ID for a different chat.";
  }
  $("guide-settings-link").href = basePath + "?page=settings";
  $("setup-view").querySelectorAll("[data-board-link]").forEach((link) => { link.href = basePath; });
  $("setup-view").querySelectorAll("[data-settings-link]").forEach((link) => { link.href = basePath + "?page=settings"; });
  $("setup-view").querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const snippet = $(button.dataset.copyTarget);
      const status = [...$("setup-view").querySelectorAll("[data-copy-status]")].find((item) => item.dataset.copyStatus === button.dataset.copyTarget);
      button.disabled = true;
      try {
        if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(snippet.textContent);
        status.textContent = "Copied to clipboard.";
        status.classList.remove("error");
      } catch {
        snippet.focus();
        const range = document.createRange();
        range.selectNodeContents(snippet);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
        status.textContent = "Copy is unavailable here. The snippet is selected; copy it with your keyboard.";
        status.classList.add("error");
      } finally { button.disabled = false; }
    });
  });
  const tabs = [...$("setup-view").querySelectorAll('[role="tab"]')];
  const chooseTab = (selected, focus = false) => {
    tabs.forEach((tab) => {
      const active = tab === selected;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
      tab.classList.toggle("active", active);
      $(tab.getAttribute("aria-controls")).hidden = !active;
    });
    if (focus) selected.focus();
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => chooseTab(tab));
    tab.addEventListener("keydown", (event) => {
      const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
      if (next !== null) { event.preventDefault(); chooseTab(tabs[next], true); }
    });
  });
}

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
if (new URLSearchParams(location.search).get("page") === "setup" && $("setup-view")) showSetup();
else api("/me").then(authenticated).catch((error) => { showLogin(); if (error.status !== 401) notify(error.message || "Could not connect to the board.", true); });
