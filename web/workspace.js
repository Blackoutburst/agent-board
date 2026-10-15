"use strict";

// Work management is kept separate from authentication and conversation transport.
const Workspace = (() => {
  Object.assign(ticketStatuses, { todo: "To do", in_progress: "In progress", blocked: "Blocked", review: "In review", done: "Done" });
  Object.assign(ticketPriorities, { low: "Low", normal: "Normal", high: "High", urgent: "Urgent" });
  const workStates = { ready: "Ready", running: "In progress", blocked: "Blocked", waiting_on_human: "Needs human input", recovery: "Needs recovery", review: "In review", done: "Done" };
  let projectTab = "tasks";
  let listMode = "list";
  const guard = (action) => (...args) => Promise.resolve().then(() => action(...args)).catch(workError);
  const badge = (label, kind = "") => el("span", { class: `badge ${kind}` }, label);
  const status = (task) => badge(workStates[task.work_state] || ticketStatuses[task.status], `status-${task.work_state || task.status}`);
  const tabs = (items, active, change) => el("nav", { class: "tabs", "aria-label": "Project sections" }, items.map(([key, label]) => btn(label, () => change(key), key === active ? "selected" : "", { "aria-current": key === active ? "page" : null })));
  const panel = (title, ...content) => el("section", { class: "panel" }, el("div", { class: "panel-heading" }, el("h2", {}, title)), ...content);
  const box = (title, text, action) => el("div", { class: "empty-state" }, el("h2", {}, title), el("p", {}, text), action);
  function navigate(view) {
    setView(view);
    ["agents", "requests"].forEach(key => $(key + "-nav").classList.toggle("active", view === key));
  }
  function frame(title, subtitle, actions = [], breadcrumb = null) {
    document.title = `${title} · Board`;
    $("main").replaceChildren(breadcrumb || el("span"), head(title, subtitle, ...actions));
    const body = el("div", { class: "page-body" });
    $("main").append(body);
    return body;
  }
  const memberPills = (members) => el("span", { class: "member-pills" }, members.map(id => el("span", { class: "member-avatar", title: ownerName(id), "aria-label": ownerName(id) }, ownerName(id).slice(0, 2))));
  const csv = (value) => [...new Set(String(value || "").split(",").map(v => v.trim()).filter(Boolean))];

  async function projects(push = true) {
    const request = ++state.request;
    navigate("projects");
    workRoute({ page: "projects" }, push);
    const body = frame("Projects", "A shared place for goals, tasks, and the agents doing the work.", [btn("New project", () => projectEditor(), "primary")]);
    const search = field("Find a project", "query", { type: "search", value: state.projectQuery, placeholder: "Search projects…" });
    const filters = el("form", { class: "project-toolbar" }, search, check("Include archived", "archived", state.includeArchived), el("button", { type: "submit" }, "Search"));
    const list = el("div", { class: "project-list" }, empty("Loading projects…"));
    body.append(filters, list);
    async function refresh() {
      const data = await api(`/projects?${new URLSearchParams({ query: state.projectQuery, include_archived: String(state.includeArchived) })}`);
      if (request !== state.request) return;
      state.projects = data;
      list.replaceChildren(...(data.length ? data.map(p => {
        const total = p.ticket_counts.total || 0, done = p.ticket_counts.done || 0;
        const article = el("article", { class: "project-list-item" },
          el("div", { class: "project-list-title" }, el("h2", {}, workLink(p.name, { project: p.id })), p.archived ? badge("Archived") : badge("Private"), memberPills(p.participants)),
          el("p", { class: "muted" }, p.description || "Add a short description to give your team a clear goal."),
          el("div", { class: "project-list-footer" }, el("span", {}, `${done} of ${total} tasks complete`),
            p.open_human_requests ? workLink(`${p.open_human_requests} needs input`, { page: "requests", project_filter: p.id }, "needs-input-link") : null,
            el("span", { class: "muted" }, `Updated ${date(p.updated_at)}`)),
          el("progress", { max: total || 1, value: done, "aria-label": `${p.name}: ${done} of ${total} tasks complete` }));
        return article;
      }) : [box("Create your first project", "Describe a goal, invite the people involved, and break it into tasks.", btn("New project", () => projectEditor(), "primary"))]));
    }
    submit(filters, async data => { state.projectQuery = String(data.get("query")).trim(); state.includeArchived = data.has("archived"); await refresh(); });
    await refresh();
  }

  async function project(id, push = true) {
    const request = ++state.request;
    navigate("project");
    workRoute({ project: id }, push);
    const p = await api(`/projects/${workPath(id)}`);
    if (request !== state.request) return;
    state.project = p;
    const body = frame(p.name, p.description, [btn("Project settings", () => projectEditor(p)), btn("New task", () => ticketEditor(null, p), "primary", { disabled: p.archived })], workBack(p));
    const section = el("div", { class: "project-section" });
    const tabItems = [["tasks", "Tasks"], ["overview", "Overview"], ["activity", "Activity"], ["agents", "Agents"]];
    let tabNav;
    const change = async tab => {
      projectTab = tab;
      const replacement = tabs(tabItems, projectTab, guard(change));
      tabNav.replaceWith(replacement); tabNav = replacement;
      section.replaceChildren(empty("Loading…")); await render();
    };
    tabNav = tabs(tabItems, projectTab, guard(change));
    body.append(tabNav, section);
    async function render() {
      if (p.archived) section.prepend(el("p", { class: "callout" }, "This project is archived. Restore it in project settings to add work."));
      if (projectTab === "overview") {
        const main = el("div", {},
          panel("Goal and shared context", el("div", { class: "panel-body prose" }, p.context || "Write down the goal, constraints, and decisions here.")),
          panel("Environment", el("div", { class: "panel-body prose" }, p.environment || "No environment details yet.")));
        const details = el("div", { class: "panel-body" },
          el("h3", {}, "Repository"), repositoryLink(p.repository_url),
          el("h3", {}, "People"), el("p", {}, p.participants.map(ownerName).join(", ")),
          el("h3", {}, "Agent work queue"), badge(p.automation_enabled ? "Enabled" : "Paused", p.automation_enabled ? "status-ready" : ""),
          el("p", { class: "small muted" }, "Authorized running agents can pick ready work. Idle chats check on their polling schedule."));
        section.replaceChildren(el("div", { class: "overview-grid" }, main, el("aside", {}, panel("Project", details))));
      } else if (projectTab === "activity") {
        const events = await api(`/activity?project_id=${workPath(id)}`);
        if (request === state.request) section.replaceChildren(activityList(events));
      } else if (projectTab === "agents") {
        const entries = await api(`/agents?project_id=${workPath(id)}`);
        if (request === state.request) section.replaceChildren(agentList(entries));
      } else {
        const tasks = await api(`/projects/${workPath(id)}/tickets`);
        if (request === state.request) renderTasks(section, p, tasks);
      }
    }
    await render();
  }

  function matches(task, filter) {
    if (filter.status && task.status !== filter.status) return false;
    if (filter.priority && task.priority !== filter.priority) return false;
    const text = `${task.title} ${task.description} ${task.number} ${task.assignee_agent_name || ""}`.toLowerCase();
    return filter.query.toLowerCase().split(/\s+/).filter(Boolean).every(token => {
      if (token === "is:open") return task.status !== "done";
      if (token === "is:done") return task.status === "done";
      if (token === "is:ready") return task.ready;
      if (token === "is:blocked") return ["blocked", "waiting_on_human", "recovery"].includes(task.work_state);
      if (token === "assignee:me") return task.assignee_owner_id === state.actor.owner_id || task.claimant?.owner_id === state.actor.owner_id;
      if (token.startsWith("label:")) return task.labels.some(v => v.toLowerCase() === token.slice(6));
      return text.includes(token);
    });
  }
  function renderTasks(container, p, tasks) {
    const filter = getTicketFilters(p.id);
    const count = el("span", { class: "muted small", "aria-live": "polite" });
    const list = el("div", { class: "issue-list" });
    const query = field("Search tasks", "query", { type: "search", value: filter.query, placeholder: "Search tasks…  is:ready  assignee:me  label:backend" });
    const controls = el("form", { class: "task-filters" }, query, select("Status", "status", [["", "All statuses"], ...Object.entries(ticketStatuses)], filter.status), select("Priority", "priority", [["", "All priorities"], ...Object.entries(ticketPriorities)], filter.priority), el("button", { type: "submit" }, "Filter"));
    const update = () => {
      const visible = tasks.filter(t => matches(t, filter));
      count.textContent = `${visible.length} task${visible.length === 1 ? "" : "s"}`;
      if (listMode === "board") {
        list.className = "kanban";
        list.replaceChildren(...Object.entries(ticketStatuses).map(([key, label]) => el("section", { class: "kanban-column" }, el("h3", {}, label, badge(String(visible.filter(t => t.status === key).length))), visible.filter(t => t.status === key).map(taskRow))));
      } else {
        list.className = "issue-list";
        list.replaceChildren(...(visible.length ? visible.map(taskRow) : [box(tasks.length ? "No matching tasks" : "Turn your goal into work", tasks.length ? "Try a different search or clear your filters." : "Add a task with a clear outcome. Agents can then discover it in the work queue.", tasks.length ? btn("Clear filters", () => { Object.assign(filter, { query: "", status: "", priority: "" }); renderTasks(container, p, tasks); }) : btn("New task", () => ticketEditor(null, p), "primary"))]));
      }
    };
    submit(controls, async data => { ["query", "status", "priority"].forEach(key => filter[key] = String(data.get(key) || "")); update(); });
    container.replaceChildren(controls, el("div", { class: "list-heading" }, count,
      el("div", { class: "segmented", "aria-label": "Task layout" }, btn("List", () => { listMode = "list"; renderTasks(container, p, tasks); }, listMode === "list" ? "selected" : ""), btn("Board", () => { listMode = "board"; renderTasks(container, p, tasks); }, listMode === "board" ? "selected" : ""))), list);
    update();
  }
  function taskRow(t) {
    const assignee = t.claimant?.agent_name || t.assignee_agent_name || (t.assignee_owner_id ? ownerName(t.assignee_owner_id) : "Anyone");
    return el("article", { class: "issue-row" }, el("span", { class: `issue-icon ${t.status === "done" ? "done" : ""}`, "aria-label": ticketStatuses[t.status] }, t.status === "done" ? "✓" : "◉"),
      el("div", { class: "issue-content" }, el("div", { class: "issue-title" }, workLink(t.title, { ticket: t.id }), t.labels.map(label => badge(label, "label-badge"))),
        el("div", { class: "issue-meta" }, `#${t.number}`, el("span", {}, `opened ${date(t.created_at)}`), t.blocked_by.length ? el("span", { class: "blocked-label" }, `Blocked by ${t.blocked_by.map(d => "#" + d.number).join(", ")}`) : null, t.progress ? el("span", { class: "progress-preview" }, t.progress) : null)),
      el("div", { class: "issue-trailing" }, status(t), t.priority !== "normal" ? badge(ticketPriorities[t.priority], `priority-${t.priority}`) : null, el("span", { class: "assigned-label", title: assignee }, assignee)));
  }

  async function ticket(id, push = true) {
    const request = ++state.request;
    navigate("ticket");
    workRoute({ ticket: id }, push);
    const info = await api(`/tickets/${workPath(id)}`);
    const [discussion, events] = await Promise.all([api(`/threads/${workPath(info.ticket.thread_id)}`), api(`/activity?ticket_id=${workPath(id)}`)]);
    if (request !== state.request) return;
    state.ticket = info.ticket; state.project = info.project;
    state.threadId = info.ticket.thread_id; state.threadData = discussion;
    renderTicket(info, events);
    await api(`/threads/${workPath(state.threadId)}/read`, { method: "POST", data: {} });
  }
  function renderTicket({ ticket: t, project: p }, events = []) {
    const actions = [btn("Edit task", () => ticketEditor(t, p)), ...(t.status === "done" ? [] : [btn("Delegate subtask", () => delegate(t)), btn("Ask a human", () => askHuman(t, p))])];
    const body = frame(t.title, `#${t.number} in ${p.name}`, actions, workBack(p, t));
    body.prepend(el("div", { class: "task-state-line" }, status(t), el("span", { class: "muted small" }, `Updated ${date(t.updated_at)}`)));
    const sidebar = el("aside", { class: "task-sidebar" },
      sideSection("Assigned to", el("p", {}, t.assignee_agent_name || (t.assignee_owner_id ? ownerName(t.assignee_owner_id) : "Any available agent")), t.claimant ? el("div", { class: "claim-details" }, el("strong", {}, t.claimant.agent_name || authorLabel(t.claimant)), el("span", {}, t.claimant.device_label || ""), el("span", {}, t.claimant.session_label || ""), t.lease_expires_at ? el("span", { class: t.claim_expired ? "error" : "muted" }, `${t.claim_expired ? "Expired" : "Lease until"} ${date(t.lease_expires_at)}`) : null) : el("p", { class: "small muted" }, "No active worker")),
      sideSection("Priority", badge(ticketPriorities[t.priority], `priority-${t.priority}`)),
      sideSection("Blocked by", dependencyLinks(t.dependencies)), sideSection("Blocks", dependencyLinks(t.blocks)),
      sideSection("Labels", t.labels.length ? t.labels.map(label => badge(label, "label-badge")) : el("span", { class: "muted" }, "None")),
      t.required_skills.length ? sideSection("Required skills", t.required_skills.map(skill => badge(skill))) : null,
      t.resource ? sideSection("Shared resource", el("code", {}, t.resource)) : null,
      btn("Edit assignments & dependencies", () => ticketEditor(t, p), "quiet full-width"));
    if (t.status !== "done") {
      if (t.claimant) sidebar.prepend(btn("Release claim", () => releaseTask(t), "full-width"));
      else if (!t.waiting_on_human && !t.blocked_by.length) sidebar.prepend(btn(t.claim_expired ? "Recover task" : "Start work", guard(async () => {
        await api(`/tickets/${workPath(t.id)}/claim`, { method: "POST", data: { expected_version: t.version } }); await ticket(t.id, false);
      }), "full-width"));
    }
    const discussion = panel("Discussion", el("div", { id: "older-messages" }), el("div", { id: "messages", class: "messages" }), composer());
    const main = el("div", {}, panel("Description", el("div", { class: "panel-body prose" }, t.description || "No description provided.")),
      t.acceptance_criteria ? panel("Acceptance criteria", el("div", { class: "panel-body prose" }, t.acceptance_criteria)) : null,
      t.progress ? el("div", { class: "callout" }, el("strong", {}, "Latest progress"), el("p", {}, t.progress)) : null,
      t.human_requests.filter(r => r.status === "open").map(r => requestCard(r, t)),
      t.outcome || t.evidence ? panel("Outcome & verification", el("div", { class: "panel-body" }, el("p", { class: "prose" }, t.outcome), el("p", { class: "prose muted" }, t.evidence))) : null,
      discussion, el("details", { class: "activity-details" }, el("summary", {}, "Task activity"), activityList(events)));
    body.append(el("div", { class: "task-detail-layout" }, main, sidebar));
    renderMessages();
  }
  const sideSection = (title, ...content) => el("section", {}, el("h2", {}, title), ...content.flat());
  function dependencyLinks(tasks) {
    return tasks.length ? el("ul", { class: "dependency-links" }, tasks.map(t => el("li", {}, el("span", { class: t.status === "done" ? "done" : "muted" }, t.status === "done" ? "✓" : "◉"), workLink(`#${t.number} ${t.title}`, { ticket: t.id })))) : el("p", { class: "muted" }, "None");
  }
  function releaseTask(t) {
    const form = el("form", {}, el("p", {}, "Release this claim so the next eligible worker can take over. Current progress and history are retained."), formButtons("Release claim"));
    submit(form, async () => { await api(`/tickets/${workPath(t.id)}`, { method: "PATCH", data: { expected_version: t.version, release_claim: true } }); closeModal(); await ticket(t.id, false); });
    openModal("Release task", form);
  }

  function bindForm(form, action) {
    const error = el("div", { class: "form-error", role: "alert", hidden: true });
    form.prepend(error);
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const buttons = [...form.querySelectorAll('button[type="submit"]')]; buttons.forEach(b => b.disabled = true);
      error.hidden = true;
      try { await action(new FormData(form)); }
      catch (failure) { error.hidden = false; error.textContent = failure.status === 409 ? "This item changed while you were editing. Your draft is still here. Open the current item in another tab to compare, then reopen the editor before saving." : failure.message; }
      finally { buttons.forEach(b => b.disabled = false); }
    });
  }
  async function projectEditor(p = null) {
    const key = idempotencyKey();
    const form = el("form", { class: "work-form" }, field("Project name", "name", { value: p?.name || "", required: true }), field("Description", "description", { value: p?.description || "", placeholder: "What is this project trying to achieve?" }), field("Goal, context & decisions", "context", { type: "textarea", rows: 6, value: p?.context || "" }),
      field("Repository URL", "repository_url", { type: "url", value: p?.repository_url || "", placeholder: "https://github.com/owner/repository" }), field("Environment", "environment", { type: "textarea", value: p?.environment || "" }),
      p ? el("div", {}, check("Allow ready work in the agent queue", "automation_enabled", p.automation_enabled), check("Archive this project", "archived", p.archived)) : el("fieldset", {}, el("legend", {}, "People"), state.owners.map(o => check(o.display_name, "participants", true, o.id || o.owner_id))), formButtons(p ? "Save project" : "Create project"));
    bindForm(form, async data => {
      const payload = Object.fromEntries(["name", "description", "context", "repository_url", "environment"].map(key => [key, String(data.get(key)).trim()]));
      if (p) Object.assign(payload, { expected_version: p.version, automation_enabled: data.has("automation_enabled"), archived: data.has("archived") });
      else Object.assign(payload, { participants: data.getAll("participants"), idempotency_key: key });
      const saved = await api(p ? `/projects/${workPath(p.id)}` : "/projects", { method: p ? "PATCH" : "POST", data: payload });
      closeModal(); projectTab = "tasks"; await project(saved.id); notify(p ? "Project updated." : "Project created.");
    });
    openModal(p ? "Project settings" : "New project", form);
  }
  async function ticketEditor(t, p) {
    const [allTasks, agentEntries] = await Promise.all([api(`/projects/${workPath(p.id)}/tickets`), api(`/agents?project_id=${workPath(p.id)}`)]);
    const key = idempotencyKey();
    const who = t?.assignee_agent_id ? "agent:" + t.assignee_agent_id : t?.assignee_owner_id ? "owner:" + t.assignee_owner_id : "";
    const dependencyPicker = el("fieldset", { class: "dependency-picker" }, el("legend", {}, "Blocked by"), allTasks.filter(task => task.id !== t?.id).map(task => check(`#${task.number} ${task.title}`, "depends_on", t?.depends_on?.includes(task.id), task.id)));
    if (allTasks.length <= (t ? 1 : 0)) dependencyPicker.append(el("p", { class: "small muted" }, "No other tasks in this project yet."));
    const form = el("form", { class: "work-form" }, field("Task title", "title", { value: t?.title || "", required: true, placeholder: "Describe the result you want" }), field("Description", "description", { type: "textarea", rows: 5, value: t?.description || "" }), field("Acceptance criteria", "acceptance_criteria", { type: "textarea", rows: 3, value: t?.acceptance_criteria || "", placeholder: "What needs to be true when this is complete?" }),
      el("div", { class: "form-row" }, select("Priority", "priority", Object.entries(ticketPriorities), t?.priority || "normal"), select("Assigned to", "assignee", [["", "Any available agent"], ...p.participants.map(id => ["owner:" + id, ownerName(id)]), ...agentEntries.filter(a => !a.id.startsWith("device:")).map(a => ["agent:" + a.id, a.name])], who)),
      t ? select("Status", "status", Object.entries(ticketStatuses), t.status) : null,
      el("details", { class: "metadata-editor" }, el("summary", {}, "Dependencies, labels & work requirements"), dependencyPicker, field("Labels", "labels", { value: (t?.labels || []).join(", "), placeholder: "backend, ux, documentation" }), field("Required skills", "required_skills", { value: (t?.required_skills || []).join(", "), placeholder: "Optional: python, android" }), field("Shared resource", "resource", { value: t?.resource || "", hint: "Tasks using the same resource cannot run at the same time.", placeholder: "Optional: agent-board:main" })),
      t ? el("details", { class: "metadata-editor", open: t.status === "done" ? "" : null }, el("summary", {}, "Outcome & verification"), field("Outcome", "outcome", { type: "textarea", value: t.outcome }), field("Verification evidence", "evidence", { type: "textarea", value: t.evidence, hint: "Required together with an outcome when marking a task done." })) : null,
      formButtons(t ? "Save task" : "Create task"));
    bindForm(form, async data => {
      const payload = Object.fromEntries(["title", "description", "acceptance_criteria", "priority", "resource"].map(key => [key, String(data.get(key) || "").trim()]));
      const assignee = String(data.get("assignee") || "");
      Object.assign(payload, { assignee_owner_id: assignee.startsWith("owner:") ? assignee.slice(6) : null, assignee_agent_id: assignee.startsWith("agent:") ? assignee.slice(6) : null, depends_on: data.getAll("depends_on"), labels: csv(data.get("labels")), required_skills: csv(data.get("required_skills")) });
      if (t) {
        Object.assign(payload, { expected_version: t.version, status: data.get("status"), outcome: data.get("outcome"), evidence: data.get("evidence") });
        if (t.status === "done" && payload.status !== "done") {
          if (payload.outcome === t.outcome) payload.outcome = "";
          if (payload.evidence === t.evidence) payload.evidence = "";
        }
      }
      else payload.idempotency_key = key;
      const saved = await api(t ? `/tickets/${workPath(t.id)}` : `/projects/${workPath(p.id)}/tickets`, { method: t ? "PATCH" : "POST", data: payload });
      closeModal(); await ticket(saved.id); notify(t ? "Task updated." : "Task created.");
    });
    openModal(t ? `Edit task #${t.number}` : "New task", form);
  }

  function askHuman(t, p) {
    const key = idempotencyKey();
    const form = el("form", {}, select("Who can help?", "owner_id", p.participants.map(id => [id, ownerName(id)]), state.actor.owner_id), select("Request type", "kind", [["question", "Information"], ["action", "An action"], ["approval", "Approval"]]), field("What do you need?", "question", { type: "textarea", rows: 5, required: true, placeholder: "Ask a specific question or describe the action and how to verify it." }), el("p", { class: "small muted" }, "This task will wait for the selected person. Its current worker can then take other work."), formButtons("Request input"));
    bindForm(form, async data => { await api(`/tickets/${workPath(t.id)}/input`, { method: "POST", data: { owner_id: data.get("owner_id"), kind: data.get("kind"), question: data.get("question"), idempotency_key: key } }); closeModal(); await ticket(t.id, false); await refreshRequestCount(); });
    openModal("Ask a human", form);
  }
  function requestCard(r, t = null) {
    const card = el("article", { class: "human-request" }, el("div", { class: "work-card-heading" }, badge(r.kind === "question" ? "Information needed" : r.kind === "action" ? "Action needed" : "Approval needed", "status-waiting_on_human"), el("span", { class: "small muted" }, `For ${ownerName(r.owner_id)}`)),
      t ? null : workLink(`#${r.number} ${r.title}`, { ticket: r.ticket_id }), el("p", { class: "prose" }, r.question));
    if (r.status === "resolved") card.append(el("p", { class: "prose" }, r.answer));
    else if (r.owner_id === state.actor.owner_id) {
      const form = el("form", {}, field("Your response", "answer", { type: "textarea", required: true, placeholder: "Provide the answer or describe the action you completed." }), el("button", { type: "submit", class: "primary" }, "Answer & unblock"));
      bindForm(form, async data => { await api(`/human-requests/${workPath(r.id)}/resolve`, { method: "POST", data: { answer: data.get("answer") } }); await refreshRequestCount(); if (t) await ticket(t.id, false); else await requests(); notify("Response saved. The task can continue when its other prerequisites are complete."); });
      card.append(form);
    } else card.append(el("p", { class: "small muted" }, `Waiting for ${ownerName(r.owner_id)} to respond.`));
    return card;
  }
  async function refreshRequestCount() {
    if (!state.actor) return;
    const data = await api("/human-requests");
    const count = data.filter(r => r.owner_id === state.actor.owner_id).length;
    $("request-count").textContent = String(count); $("request-count").hidden = count === 0;
  }
  async function requests() {
    const request = ++state.request;
    navigate("requests"); workRoute({ page: "requests" });
    const body = frame("Needs you", "Questions, approvals, and actions that keep your projects moving.", [btn("Refresh", guard(requests))]);
    const data = await api("/human-requests");
    if (request !== state.request) return;
    const mine = data.filter(r => r.owner_id === state.actor.owner_id), others = data.filter(r => r.owner_id !== state.actor.owner_id);
    body.append(...(mine.length ? mine.map(r => requestCard(r)) : [box("You're all caught up", "Your agents have no open requests for you.")]));
    if (others.length) body.append(panel("Waiting on other people", ...others.map(r => requestCard(r))));
    await refreshRequestCount();
  }
  function activityList(events) {
    const descriptions = { ticket_created: "created this task", ticket_claimed: "started work", ticket_recovered: "recovered an expired claim", ticket_updated: "updated this task", ticket_progress: "reported progress", ticket_delegated: "delegated a subtask", ticket_unblocked: "unblocked this task", human_input_requested: "requested human input", human_input_resolved: "answered a human request" };
    if (!events.length) return box("No activity yet", "Claims, progress, handoffs, and completed work will appear here.");
    const rows = events.map(event => {
      const detail = el("div", {},
        el("p", {}, el("strong", {}, event.actor.agent_name || authorLabel(event.actor)),
          ` ${descriptions[event.event] || event.event.replaceAll("_", " ")}`,
          workLink(` #${event.number}`, { ticket: event.ticket_id })),
        event.detail.progress ? el("p", { class: "prose muted" }, event.detail.progress) : null,
        el("time", { class: "small muted" }, date(event.created_at)));
      return el("li", {}, el("span", { class: "activity-dot" }), detail);
    });
    return el("ol", { class: "activity-list" }, rows);
  }

  function agentList(entries) {
    return entries.length ? el("div", { class: "agent-list" }, entries.map(a => el("article", { class: "agent-card" },
      el("div", { class: "agent-heading" }, el("div", { class: `agent-emblem ${a.agent_kind}` }, a.agent_kind.includes("claude") ? "C" : "CX"), el("div", {}, el("h2", {}, a.name), el("p", { class: "small muted" }, `${ownerName(a.owner_id)} / ${a.device_label}`)), badge(a.online ? "Online" : "Offline", a.online ? "status-ready" : "")),
      el("p", { class: "small muted" }, a.last_seen_at ? `Last seen ${date(a.last_seen_at)}` : "No recent check-in"),
      a.tickets.length ? a.tickets.map(t => el("div", { class: "agent-task" }, workLink(`#${t.number} ${t.title}`, { ticket: t.id }), status(t))) : el("p", { class: "muted" }, "No active tasks"),
      el("details", {}, el("summary", {}, `${a.sessions.length} chat session${a.sessions.length === 1 ? "" : "s"}`), a.sessions.map(s => el("div", { class: "session-row" }, el("strong", {}, s.label), badge(s.autonomous ? "Worker" : "Observer"), el("span", { class: "small muted" }, s.skills.join(", ") || "General")))),
      a.owner_id === state.actor.owner_id && !a.id.startsWith("device:") ? btn("Rename", () => renameAgent(a), "quiet") : null))) : box("Connect your first agent", "Each installation gets a persistent name. Codex and Claude Code on the same machine remain separate agents.", btn("Connect agent", guard(setup), "primary"));
  }
  async function agents() {
    const request = ++state.request; navigate("agents"); workRoute({ page: "agents" });
    const body = frame("Agents", "Persistent identities, live sessions, and the work each agent owns.", [btn("Refresh", guard(agents)), btn("Connect agent", guard(setup), "primary")]);
    const entries = await api("/agents"); if (request === state.request) body.append(agentList(entries));
  }
  function renameAgent(a) {
    const form = el("form", {}, field("Agent name", "name", { value: a.name, required: true }), formButtons("Save name"));
    bindForm(form, async data => { await api(`/agents/${workPath(a.id)}`, { method: "PATCH", data: { name: data.get("name") } }); closeModal(); await agents(); });
    openModal("Rename agent", form);
  }

  async function setup() {
    if (!state.actor) {
      try { return await authenticated(await api("/me")); }
      catch { $("app").hidden = true; $("login-view").hidden = true; $("setup-view").hidden = false; $("setup-view").replaceChildren(el("section", { class: "login-card" }, el("h1", {}, "Connect an agent"), el("p", {}, "Sign in to create a one-time pairing code for your agent installation."), el("a", { href: basePath, class: "primary button-link" }, "Open the board"))); return; }
    }
    navigate("setup"); $("app").hidden = false; $("setup-view").hidden = true; workRoute({ page: "setup" });
    const body = frame("Connect an agent", "One pairing code. A persistent identity. No environment-variable setup.");
    const result = el("div", { class: "pairing-result", "aria-live": "polite" });
    const form = el("form", {}, field("Computer name", "label", { required: true, placeholder: "Work Mac, Home PC, Build server…" }), el("button", { type: "submit", class: "primary" }, "Generate pairing code"));
    bindForm(form, async data => {
      const paired = await api("/settings/pairings", { method: "POST", data: { label: data.get("label"), capabilities: [] } });
      result.replaceChildren(el("p", {}, "Enter this code when the connector asks. It expires in 10 minutes and can be used once."), el("code", { class: "pairing-code" }, paired.code), el("p", { class: "small muted" }, "Treat the code as a temporary sign-in credential. Do not post it in a task or conversation."));
    });
    const download = el("div", { class: "panel-body" },
      el("p", {}, "Download and inspect the connector, then run it on the computer where your agent lives."),
      el("a", { href: basePath + "connect.py", download: "board-connect.py", class: "button-link" }, "Download connector"),
      el("pre", {}, "python3 ~/Downloads/board-connect.py"),
      el("p", {}, "For Claude Code on the same computer:"),
      el("pre", {}, "python3 ~/Downloads/board-connect.py --client claude-code"),
      el("p", { class: "small muted" }, "Run the connector separately for each client. It keeps a stable installation ID and stores credentials in a private local file."));
    const participation = el("div", { class: "panel-body" },
      el("p", {}, "Restart your client, then tell it to read the board. Choose observer mode for notifications, or explicitly authorize worker mode for a project."),
      el("pre", { class: "prose" }, "Register this chat with the board, read my project work, and tell me what is ready or needs my input."),
      el("p", { class: "muted" }, "A mention does not wake an idle Codex chat. Keep the existing ten-minute scheduled checks, or opt a chat into the documented worker polling workflow."),
      el("a", { href: basePath + "onboarding.md" }, "Read the setup and worker guide"));
    body.append(el("div", { class: "connect-layout" },
      panel("1. Name this computer", el("div", { class: "panel-body" }, form, result)),
      panel("2. Run the connector", download)), panel("3. Start participating", participation));
  }
  function delegate(t) {
    const key = idempotencyKey();
    const form = el("form", {},
      field("Subtask title", "title", { required: true, placeholder: "Run the Linux-only verification" }),
      field("Context and work needed", "description", { type: "textarea", required: true, rows: 5, placeholder: "Explain what another agent should do, what is already done, and the result you need back." }),
      field("Required skills", "required_skills", { placeholder: "linux, python", hint: "The task is offered only to agents advertising these skills. Use windows, macos, or linux for operating systems." }),
      field("Acceptance criteria", "acceptance_criteria", { type: "textarea", rows: 3 }),
      field("Shared resource", "resource", { placeholder: "Optional resource that must be used exclusively" }),
      el("p", { class: "small muted" }, "The parent task will wait for this subtask. After the subtask is complete, the original agent can resume the parent."),
      formButtons("Delegate subtask"));
    bindForm(form, async data => {
      const result = await api(`/tickets/${workPath(t.id)}/delegate`, { method: "POST", data: {
        expected_version: t.version, title: data.get("title"), description: data.get("description"),
        required_skills: csv(data.get("required_skills")), acceptance_criteria: data.get("acceptance_criteria"),
        resource: data.get("resource"), idempotency_key: key
      } });
      closeModal(); await ticket(t.id, false); notify(`Subtask #${result.child.number} created. The parent is waiting for it.`);
    });
    openModal("Delegate to another agent", form);
  }
  async function settings() {
    const request = ++state.request; navigate("settings"); workRoute({ page: "settings" });
    const body = frame("Settings", "Control participation and manage the devices connected to your account.", [btn("Connect agent", guard(setup), "primary")]);
    const [data, notifications] = await Promise.all([api("/settings"), api("/notifications").catch(() => null)]);
    if (request !== state.request) return;
    state.settings = data;
    const policy = data.policy;
    const form = el("form", {},
      check("Enable agents for my account", "enabled", policy.enabled),
      check("Read projects, tasks, and discussions", "allow_read", policy.allow_read),
      check("Post messages and discuss work", "allow_reply", policy.allow_reply),
      check("Create, claim, delegate, and update work", "allow_requests", policy.allow_requests),
      el("details", { class: "metadata-editor" }, el("summary", {}, "Legacy action scopes"),
        el("p", { class: "small muted" }, "These scopes apply to the original action tasks. Project task coordination uses the permissions above. External tools still enforce their own permissions."),
        field("Allowed actions", "actions", { type: "textarea", value: policy.actions.join("\n") }),
        field("Allowed resources", "resources", { type: "textarea", value: policy.resources.join("\n") })),
      el("button", { type: "submit", class: "primary" }, "Save permissions"));
    bindForm(form, async values => {
      await api("/settings/policy", { method: "PUT", data: { enabled: values.has("enabled"), allow_read: values.has("allow_read"), allow_reply: values.has("allow_reply"), allow_requests: values.has("allow_requests"), actions: lines(values.get("actions")), resources: lines(values.get("resources")) } });
      notify("Participation settings saved.");
    });
    const devices = el("div", { class: "panel-body" }, data.devices.length ? data.devices.map(device => el("div", { class: "device-row" },
      el("div", { class: "device-header" }, el("strong", {}, device.label), device.revoked ? badge("Revoked") : btn("Revoke", () => {
        const confirm = el("form", {}, el("p", {}, `Disconnect ${device.label}? It will need a new pairing code to reconnect.`), formButtons("Revoke device"));
        bindForm(confirm, async () => { await api(`/settings/devices/${workPath(device.id)}`, { method: "DELETE" }); closeModal(); await settings(); });
        openModal("Revoke device", confirm);
      }, "quiet")), el("p", { class: "small muted" }, `Connected ${date(device.created_at)}`))) : el("p", { class: "muted" }, "No devices connected."));
    body.append(el("div", { class: "overview-grid" }, panel("Agent participation", el("div", { class: "panel-body" }, form)),
      el("aside", {}, panel("Notifications", el("div", { class: "panel-body" }, badge(notifications?.configured ? "Discord connected" : "In-app notifications", notifications?.configured ? "status-ready" : ""), el("p", { class: "small muted" }, "Human requests always appear in Needs you. Configured Discord alerts reach the requested person even when their agent is offline."), notifications?.pending ? el("p", {}, `${notifications.pending} notifications queued`) : null)))), panel("Connected devices", devices));
  }
  async function inbox() {
    const request = ++state.request; navigate("inbox"); workRoute({ page: "inbox" });
    const body = frame("Inbox", "Ready work, mentions, and conversations across your projects.", [btn("Refresh", guard(inbox))]);
    const data = await api(`/inbox${state.inboxCursor ? '?cursor='+workPath(state.inboxCursor) : ''}`);
    if (request !== state.request) return;
    state.inboxCursor = data.cursor;
    const messages = new Map(state.inboxMessages.map(m => [m.id, m]));
    data.messages.forEach(m => messages.set(m.id, m));
    state.inboxMessages = [...messages.values()].sort((a,b) => b.created_at-a.created_at);
    const ready = data.work?.ready || [];
    body.append(panel("Ready work", ready.length ? el("div", { class: "issue-list" }, ready.map(taskRow)) : box("No ready work", "Tasks will appear here when their prerequisites are satisfied.")));
    const discussions = state.inboxMessages.map(message => {
      const item = btn("", guard(() => openThread(message.thread_id)), "inbox-message");
      item.append(authorMeta(message.author, message.created_at), el("p", { class: "prose" }, message.body));
      if (message.mentions?.includes(state.actor.owner_id)) item.append(badge("Mentioned you", "status-running"));
      return item;
    });
    body.append(panel("Discussions", discussions.length ? discussions : box("No new discussions", "Project conversations and mentions will appear here.")));
    if (data.tasks.length) body.append(panel("Legacy action requests", data.tasks.map(taskCard)));
    await refreshRequestCount();
  }
  $("agents-nav").addEventListener("click", guard(agents));
  $("requests-nav").addEventListener("click", guard(requests));
  $("guide-link").addEventListener("click", event => { event.preventDefault(); guard(setup)(); });
  return { projects, project, ticket, renderTicket, projectEditor, ticketEditor, agents, requests, setup, settings, inbox, refreshRequestCount };
})();
