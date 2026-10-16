"use strict";

// Work management is kept separate from authentication and conversation transport.
const Workspace = (() => {
  Object.assign(ticketStatuses, { todo: "To do", in_progress: "In progress", blocked: "Blocked", review: "In review", done: "Done" });
  Object.assign(ticketPriorities, { low: "Low", normal: "Normal", high: "High", urgent: "Urgent" });
  const workStates = { ready: "Ready", running: "In progress", blocked: "Blocked", waiting_on_human: "Needs human input", recovery: "Needs recovery", review: "In review", done: "Done" };
  let projectTab = "tasks";
  let listMode = "list";
  let agentTab = "profiles";
  let includeArchivedProfiles = false;
  const guard = (action) => (...args) => Promise.resolve().then(() => action(...args)).catch(workError);
  const badge = (label, kind = "") => el("span", { class: `badge ${kind}` }, label);
  const status = (task) => task.blocked_by?.length && task.status !== "blocked"
    ? badge(`${ticketStatuses[task.status]} · waiting for ${task.blocked_by.map(t => "#" + t.number).join(", ")}`, "status-blocked")
    : badge(workStates[task.work_state] || ticketStatuses[task.status], `status-${task.work_state || task.status}`);
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
    const archived = check("Include archived", "archived", state.includeArchived);
    const filters = el("form", { class: "project-toolbar" }, search, archived);
    const list = el("div", { class: "project-list" }, empty("Loading projects…"));
    body.append(filters, list);
    let generation = 0, debounce;
    async function refresh() {
      const current = ++generation;
      const data = await api(`/projects?${new URLSearchParams({ query: state.projectQuery, include_archived: String(state.includeArchived) })}`);
      if (request !== state.request || current !== generation) return;
      state.projects = data;
      list.replaceChildren(...(data.length ? data.map(p => {
        const total = p.ticket_counts.total || 0, done = p.ticket_counts.done || 0;
        const article = el("article", { class: "project-list-item" },
          el("div", { class: "project-list-title" }, el("h2", {}, workLink(p.name, { project: p.id })), p.archived ? badge("Archived") : badge("Private"), memberPills(p.participants)),
          Markdown.block(p.description || "Add a short description to give your team a clear goal.", "muted"),
          el("div", { class: "project-list-footer" }, el("span", {}, `${done} of ${total} tasks complete`),
            p.open_human_requests ? workLink(`${p.open_human_requests} needs input`, { page: "requests", project_filter: p.id }, "needs-input-link") : null,
            el("span", { class: "muted" }, `Updated ${date(p.updated_at)}`)),
          el("progress", { max: total || 1, value: done, "aria-label": `${p.name}: ${done} of ${total} tasks complete` }));
        return article;
      }) : [box("Create your first project", "Describe a goal, invite the people involved, and break it into tasks.", btn("New project", () => projectEditor(), "primary"))]));
    }
    submit(filters, async data => { state.projectQuery = String(data.get("query")).trim(); state.includeArchived = data.has("archived"); await refresh(); });
    search.querySelector('input').addEventListener('input', event => {
      state.projectQuery = event.target.value.trim(); ++generation; clearTimeout(debounce);
      debounce = setTimeout(() => refresh().catch(workError), 250);
    });
    archived.querySelector('input').addEventListener('change', event => { state.includeArchived = event.target.checked; clearTimeout(debounce); refresh().catch(workError); });
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
          panel("Goal and shared context", Markdown.block(p.context || "Write down the goal, constraints, and decisions here.", "panel-body")),
          panel("Environment", Markdown.block(p.environment || "No environment details yet.", "panel-body")));
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
    const main = el("div", {}, panel("Description", el("div", { class: "panel-body" }, Markdown.block(t.description || "No description provided."), el("div", { class: "attachments task-attachments", "aria-label": "Task attachments" }, (t.attachments || []).map(Conversations.attachment)))),
      t.acceptance_criteria ? panel("Acceptance criteria", Markdown.block(t.acceptance_criteria, "panel-body")) : null,
      t.progress ? el("div", { class: "callout" }, el("strong", {}, "Latest progress"), Markdown.block(t.progress)) : null,
      t.human_requests.filter(r => r.status === "open").map(r => requestCard(r, t)),
      t.outcome || t.evidence ? panel("Outcome & verification", el("div", { class: "panel-body" }, Markdown.block(t.outcome), Markdown.block(t.evidence, "muted"))) : null,
      discussion, el("details", { class: "activity-details" }, el("summary", {}, "Task activity"), activityList(events)));
    body.append(el("div", { class: "task-detail-layout" }, main, sidebar));
    renderMessages();
  }
  function refreshTicket(info) {
    const compose = $("compose"), focus = document.activeElement;
    const selection = focus?.selectionStart !== undefined ? [focus.selectionStart, focus.selectionEnd] : null;
    const scroll = window.scrollY;
    state.ticket = info.ticket; state.project = info.project;
    renderTicket(info);
    if (compose) $("compose").replaceWith(compose);
    if (focus?.isConnected) { focus.focus({preventScroll:true}); if (selection) focus.setSelectionRange(...selection); }
    window.scrollTo(0, scroll);
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
    const [allTasks, agentEntries, labels] = await Promise.all([api(`/projects/${workPath(p.id)}/tickets`), api(`/agents?project_id=${workPath(p.id)}`), api(`/projects/${workPath(p.id)}/labels`)]);
    const key = idempotencyKey();
    const uploaded = [];
    let savedTask = null, selectedFiles = [];
    const file = el('input', {type:'file',multiple:'',name:'task_files','aria-label':'Task attachments'});
    const tagOptions = el('div',{class:'tag-options'},labels.map(label => check(label.name,'labels',t?.labels?.includes(label.name),label.name)));
    const tagName = el('input',{name:'new_tag','aria-label':'New tag name',placeholder:'Create a tag…',maxLength:80});
    const tagColor = el('input',{type:'color',value:'#8250df','aria-label':'Tag color'});
    const tags = el('fieldset',{class:'tag-picker'},el('legend',{},'Tags'),tagOptions,
      el('div',{class:'tag-create'},tagName,tagColor,btn('Create tag',guard(async () => {
        if (!tagName.value.trim()) return;
        const saved = await api(`/projects/${workPath(p.id)}/labels`,{method:'POST',data:{name:tagName.value.trim(),color:tagColor.value}});
        const existing = [...tagOptions.querySelectorAll('input')].find(input => input.value.toLowerCase() === saved.name.toLowerCase());
        if (existing) existing.checked = true; else tagOptions.append(check(saved.name,'labels',true,saved.name));
        tagName.value = '';
      }))));
    const who = t?.assignee_agent_id ? "agent:" + t.assignee_agent_id : t?.assignee_owner_id ? "owner:" + t.assignee_owner_id : "";
    const assignable = agentEntries.filter(a => !a.id.startsWith("device:") && (a.status === "active" || !a.status || a.id === t?.assignee_agent_id));
    const agentOptions = assignable.map(a => ["agent:" + a.id, `${a.name}${a.status && a.status !== "active" ? ` (${a.status})` : ""}`]);
    if (t?.assignee_agent_id && !assignable.some(a => a.id === t.assignee_agent_id)) agentOptions.push([who, `${t.assignee_agent_name || "Currently assigned profile"} (not currently available)`]);
    const dependencyPicker = el("fieldset", { class: "dependency-picker" }, el("legend", {}, "Blocked by"), allTasks.filter(task => task.id !== t?.id).map(task => check(`#${task.number} ${task.title}`, "depends_on", t?.depends_on?.includes(task.id), task.id)));
    if (allTasks.length <= (t ? 1 : 0)) dependencyPicker.append(el("p", { class: "small muted" }, "No other tasks in this project yet."));
    const form = el("form", { class: "work-form" }, field("Task title", "title", { value: t?.title || "", required: true, placeholder: "Describe the result you want" }), field("Description", "description", { type: "textarea", rows: 5, value: t?.description || "" }), field("Acceptance criteria", "acceptance_criteria", { type: "textarea", rows: 3, value: t?.acceptance_criteria || "", placeholder: "What needs to be true when this is complete?" }),
      el("div", { class: "form-row" }, select("Priority", "priority", Object.entries(ticketPriorities), t?.priority || "normal"), select("Assigned to", "assignee", [["", "Any available agent"], ...p.participants.map(id => ["owner:" + id, ownerName(id)]), ...agentOptions], who)),
      t ? select("Status", "status", Object.entries(ticketStatuses), t.status) : null,
      tags, el('label',{},'Task attachments',file,el('span',{class:'field-hint'},'Any file · Up to 10 MB each · Included in the task description')),
      el("details", { class: "metadata-editor" }, el("summary", {}, "Dependencies & work requirements"), dependencyPicker, field("Required skills", "required_skills", { value: (t?.required_skills || []).join(", "), placeholder: "Optional: python, android" }), field("Shared resource", "resource", { value: t?.resource || "", hint: "Tasks using the same resource cannot run at the same time.", placeholder: "Optional: agent-board:main" })),
      t ? el("details", { class: "metadata-editor", open: t.status === "done" ? "" : null }, el("summary", {}, "Outcome & verification"), field("Outcome", "outcome", { type: "textarea", value: t.outcome }), field("Verification evidence", "evidence", { type: "textarea", value: t.evidence, hint: "Required together with an outcome when marking a task done." })) : null,
      formButtons(t ? "Save task" : "Create task"));
    bindForm(form, async data => {
      const payload = Object.fromEntries(["title", "description", "acceptance_criteria", "priority", "resource"].map(key => [key, String(data.get(key) || "").trim()]));
      const assignee = String(data.get("assignee") || "");
      Object.assign(payload, { assignee_owner_id: assignee.startsWith("owner:") ? assignee.slice(6) : null, assignee_agent_id: assignee.startsWith("agent:") ? assignee.slice(6) : null, depends_on: data.getAll("depends_on"), labels: data.getAll("labels"), required_skills: csv(data.get("required_skills")) });
      if (t) {
        if (assignee === who) { delete payload.assignee_owner_id; delete payload.assignee_agent_id; }
        Object.assign(payload, { expected_version: t.version, status: data.get("status"), outcome: data.get("outcome"), evidence: data.get("evidence") });
        if (t.status === "done" && payload.status !== "done") {
          if (payload.outcome === t.outcome) payload.outcome = "";
          if (payload.evidence === t.evidence) payload.evidence = "";
        }
      }
      else payload.idempotency_key = key;
      if (!savedTask) {
        selectedFiles = [...file.files];
        if (selectedFiles.some(item => item.size > 10*1024*1024)) throw new Error('Each attachment must be 10 MB or smaller.');
        if (selectedFiles.length + (t?.attachments?.length || 0) > 100) throw new Error('A task can have at most 100 attachments.');
        const controls = [...form.querySelectorAll('input,textarea,select,fieldset')];
        const disabled = controls.map(control => control.disabled);
        controls.forEach(control => control.disabled = true);
        try {
          savedTask = await api(t ? `/tickets/${workPath(t.id)}` : `/projects/${workPath(p.id)}/tickets`, { method: t ? "PATCH" : "POST", data: payload });
        } catch (failure) { controls.forEach((control, index) => control.disabled = disabled[index]); throw failure; }
      }
      try {
        for (let index = uploaded.length; index < selectedFiles.length; index++) {
          const formData = new FormData(); formData.append('file',selectedFiles[index]);
          formData.append('idempotency_key',`${key}-file-${index}`);
          uploaded.push(await api(`/threads/${workPath(savedTask.thread_id)}/attachments`,{method:'POST',form:formData}));
        }
        if (uploaded.length) {
          const current = (await api(`/tickets/${workPath(savedTask.id)}`)).ticket;
          const ids = (current.attachments || []).map(item => item.id);
          const merged = [...new Set([...ids, ...uploaded.map(item => item.id)])];
          if (merged.length !== ids.length) savedTask = await api(`/tickets/${workPath(savedTask.id)}`, {method:'PATCH',data:{expected_version:current.version,attachment_ids:merged}});
        }
      } catch (failure) {
        form.querySelector('button[type="submit"]').textContent = 'Retry adding files';
        throw new Error(`Task saved. Some files still need to be added. Retry to finish. ${failure.message}`);
      }
      closeModal(); await ticket(savedTask.id); notify(t ? "Task updated." : "Task created.");
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
      t ? null : workLink(`#${r.number} ${r.title}`, { ticket: r.ticket_id }), Markdown.block(r.question));
    if (r.status === "resolved") card.append(Markdown.block(r.answer));
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
    const mine = data.filter(r => r.owner_id === state.actor.owner_id);
    body.append(...(mine.length ? mine.map(r => requestCard(r)) : [box("You're all caught up", "Your agents have no open requests for you.")]));
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
        event.detail.progress ? Markdown.block(event.detail.progress, "muted") : null,
        el("time", { class: "small muted" }, date(event.created_at)));
      return el("li", {}, el("span", { class: "activity-dot" }), detail);
    });
    return el("ol", { class: "activity-list" }, rows);
  }

  function agentList(entries) {
    return entries.length ? el("div", { class: "agent-list" }, entries.map(a => {
      if (a.owner_id !== state.actor.owner_id) {
        return el("article", { class: "agent-card shared-agent-summary" },
          el("div", { class: "agent-heading" }, profileAvatar(a), el("div", {}, el("h2", {}, a.name), el("p", { class: "small muted" }, ownerName(a.owner_id))), profileStatus(a)),
          el("p", { class: "small muted" }, "Shared profile. Its instructions, devices, sessions and work details are private to its owner."));
      }
      const sessions = Array.isArray(a.sessions) ? a.sessions : [], tickets = Array.isArray(a.tickets) ? a.tickets : [];
      const kind = typeof a.agent_kind === "string" ? a.agent_kind : "";
      return el("article", { class: "agent-card" },
        el("div", { class: "agent-heading" }, a.avatar ? profileAvatar(a) : el("div", { class: `agent-emblem${kind.includes("claude") ? " claude-code" : ""}` }, kind.includes("claude") ? "C" : kind ? "CX" : "◇"),
          el("div", {}, el("h2", {}, a.name), el("p", { class: "small muted" }, `${ownerName(a.owner_id)} / ${a.device_label || "No executor check-in"}`)),
          badge(a.online ? "Online" : sessions.length ? "Offline" : "Not connected", a.online ? "status-ready" : "")),
        el("p", { class: "small muted" }, a.last_seen_at ? `Last seen ${date(a.last_seen_at)}` : "No executor has checked in with this profile yet."),
        tickets.length ? tickets.map(t => el("div", { class: "agent-task" }, workLink(`#${t.number} ${t.title}`, { ticket: t.id }), status(t))) : el("p", { class: "muted" }, "No active tasks"),
        el("details", {}, el("summary", {}, `${sessions.length} chat session${sessions.length === 1 ? "" : "s"}`), sessions.map(s => el("div", { class: "session-row" }, el("strong", {}, s.label), badge(s.autonomous ? "Worker" : "Observer"), el("span", { class: "small muted" }, (s.skills || []).join(", ") || "General")))),
        !a.id.startsWith("device:") ? btn("Rename", () => renameAgent(a), "quiet") : null);
    })) : box("Connect your first agent", "Each installation gets a persistent name. Codex and Claude Code on the same machine remain separate agents.", btn("Connect agent", guard(setup), "primary"));
  }
  const profileStates = { active: "Active", paused: "Paused", archived: "Archived" };
  const profileTemplates = {
    development: { name: "Developer", avatar: "🛠", role: "Development", description: "Implement focused changes and verify the result.", instructions: "Understand the selected task and existing code before making changes. Keep changes focused, run relevant checks, and explain the result and remaining limits. Ask for missing information when needed.", response_style: "Clear, concise updates with concrete verification evidence." },
    review: { name: "Reviewer", avatar: "🔎", role: "Code review", description: "Review changes for concrete defects and regressions.", instructions: "Review the selected change independently. Prioritize reproducible bugs, regressions, and missing safeguards. Cite the affected file and scenario. Explain what was checked and any limits of the review.", response_style: "Lead with actionable findings, ordered by severity." },
    research: { name: "Researcher", avatar: "📚", role: "Research", description: "Investigate a question and summarize the evidence.", instructions: "Clarify the selected question, use relevant primary sources, and distinguish verified facts from assumptions. Compare the options and explain uncertainties. Keep recommendations within the task's scope.", response_style: "A short answer followed by supporting evidence and sources." }
  };
  const profileAvatar = (p) => el("span", { class: "profile-avatar", "aria-hidden": "true", title: p.avatar || "" }, p.avatar || "◇");
  const profileStatus = (p) => badge(profileStates[p.status] || p.status, p.status === "active" ? "status-ready" : p.status === "paused" ? "status-blocked" : "");
  const profileWorkNotice = (p) => p?.active_work?.length ? el("p", { class: "callout" }, "This profile has active work. Saving this change blocks further board calls from its current sessions. Reconcile their claims before restarting with the new configuration; external processes are not stopped automatically.") : null;
  const profileField = (label, name, maximum, options = {}) => {
    const node = field(label, name, options);
    node.querySelector("input, textarea").maxLength = maximum;
    return node;
  };
  function profileCard(p) {
    const own = p.owner_id === state.actor.owner_id;
    const card = el("article", { class: "profile-card", "data-profile-id": p.id },
      el("div", { class: "profile-heading" }, profileAvatar(p), el("div", { class: "profile-identity" }, el("h2", {}, p.name),
        el("p", { class: "small muted" }, ownerName(p.owner_id), own ? " · Your profile" : " · Shared profile")), profileStatus(p)));
    if (!own) {
      card.append(el("p", { class: "muted small" }, "This profile belongs to another project participant. Its configuration and connections are private."));
      return card;
    }
    card.append(el("p", { class: "profile-description" }, p.description || "Add a description to explain this agent’s focus."),
      el("div", { class: "profile-meta" }, el("span", {}, p.role || "General purpose"), el("span", {}, `Configuration v${p.version}`),
        p.legacy ? badge("Existing installation") : null),
      el("p", { class: "small muted" }, `${p.project_ids === null ? "Existing account project scope" : `${(p.project_ids || []).length} followed project${p.project_ids?.length === 1 ? "" : "s"}`} · ${(p.bindings || []).filter(b => !b.revoked).length} connected executor${p.bindings?.filter(b => !b.revoked).length === 1 ? "" : "s"}`));
    const work = p.active_work || [];
    card.append(el("details", { class: "profile-work", "aria-label": "Current work" }, el("summary", {}, `Current work (${work.length})`),
      work.length ? work.map(item => el("div", { class: "profile-work-item" },
        item.kind === "ticket" ? workLink(item.title, { ticket: item.id }) : el("span", {}, item.title),
        el("span", { class: "small muted" }, ticketStatuses[item.status] || item.status),
        Number.isSafeInteger(item.profile_version) && item.profile_version > 0 ? btn(`Captured configuration v${item.profile_version}`, guard(() => profileHistory(p.id, item.profile_version)), "quiet profile-work-version") : null,
        item.lease_expires_at ? el("span", { class: "small muted" }, `Lease until ${date(item.lease_expires_at)}`) : null)) : el("p", { class: "small muted" }, "No active work")));
    card.append(el("div", { class: "profile-actions" },
      btn("Edit profile", guard(() => profileEditor(p.id))),
      btn("Connections", guard(() => profileBindings(p.id))),
      btn("Version history", guard(() => profileHistory(p.id)), "quiet"),
      btn("Duplicate", () => duplicateProfile(p), "quiet"),
      p.status !== "archived" ? btn(p.status === "paused" ? "Resume" : "Pause", () => changeProfileStatus(p, p.status === "paused" ? "active" : "paused"), "quiet") : null,
      btn(p.status === "archived" ? "Restore paused" : "Archive", () => changeProfileStatus(p, p.status === "archived" ? "paused" : "archived"), "quiet")));
    return card;
  }
  async function agents() {
    const request = ++state.request; navigate("agents"); workRoute({ page: "agents" });
    const body = frame("Agents", "Give each agent a purpose, then connect it to an executor on your own device.", [btn("Refresh", guard(agents)), btn("New profile", guard(() => profileEditor()), "primary")]);
    const change = guard(async tab => { agentTab = tab; await agents(); });
    const navigation = tabs([["profiles", "Profiles"], ["connections", "Connections"], ["runtime", "Runtime"]], agentTab, change);
    navigation.setAttribute("aria-label", "Agent sections");
    body.append(navigation);
    if (agentTab === "runtime") {
      await renderRuntime(body, request);
      return;
    }
    if (agentTab === "connections") {
      body.append(el("div", { class: "profile-intro" }, el("p", { class: "muted" }, "These are registered installations and their live sessions. A profile holds preferences; its executor uses the device’s existing connection."), btn("Connect a device", guard(setup))));
      const entries = await api("/agents");
      if (request === state.request) body.append(agentList(entries));
      return;
    }
    const archived = check("Include archived profiles", "archived_profiles", includeArchivedProfiles);
    archived.querySelector("input").addEventListener("change", guard(async event => { includeArchivedProfiles = event.target.checked; await agents(); }));
    body.append(el("div", { class: "profile-toolbar" }, el("p", { class: "small muted" }, "Profiles do not start a background agent. Work still needs an authorized executor."), archived));
    const loading = empty("Loading profiles…"); body.append(loading);
    const [entries, visibleAgents] = await Promise.all([api(`/agent-profiles?include_archived=${includeArchivedProfiles}`), api("/agents")]);
    if (request !== state.request) return;
    loading.remove();
    const mine = entries.filter(p => p.owner_id === state.actor.owner_id), shared = visibleAgents.filter(p => p.owner_id !== state.actor.owner_id && (includeArchivedProfiles || p.status !== "archived"));
    body.append(mine.length ? el("div", { class: "profile-grid" }, mine.map(profileCard)) : box("Create your first profile", "Start with a developer, reviewer, or researcher, then adjust its instructions and project scope.", btn("New profile", guard(() => profileEditor()), "primary")));
    if (shared.length) body.append(el("section", { class: "shared-profiles" }, el("h2", {}, "From your collaborators"), el("div", { class: "profile-grid" }, shared.map(profileCard))));
    body.append(el("p", { class: "small muted profile-permission-note" }, "Profile permissions can only narrow the access allowed in your account settings. Pausing blocks subsequent board calls; existing work or leases may still need reconciliation."));
  }
  const runtimeReasons = {
    quota_or_rate_limit: "Quota or rate limit reached", authentication_failed: "Authentication failed",
    execution_failed: "Execution failed", unavailable: "Provider unavailable", timeout: "Time limit reached",
    cleanup_unconfirmed: "Cleanup has not been confirmed", cleanup_failed: "Cleanup failed",
    runner_error: "Executor error", protocol_error: "Unexpected executor response", policy_changed: "Permissions changed",
    claim_lost: "Ticket claim lost", stop_requested: "Stop requested", owner_paused: "Owner paused execution"
  };
  function runtimeState(execution) {
    if (execution.state === "finished") {
      if (execution.cleanup_confirmed !== true) return ["Cleanup unconfirmed", "status-blocked"];
      return execution.outcome === "cancelled" ? ["Stop confirmed", "status-done"]
        : execution.outcome === "blocked" ? ["Blocked", "status-blocked"]
          : execution.outcome === "completed" ? ["Completed", "status-done"] : ["Finished", ""];
    }
    return { reserved: ["Reserved", ""], running: ["Running", "status-running"], needs_review: ["Needs review", "status-blocked"] }[execution.state] || ["Unknown state", ""];
  }
  function runtimeCard(execution, agentNames) {
    const [label, color] = runtimeState(execution), active = execution.state !== "finished";
    const stopping = active && (execution.stop_requested === true || execution.should_stop === true);
    const stopLabel = execution.stop_requested === true || execution.control_reason === "owner_paused" ? "Stop requested" : "Stop required";
    const identity = agentNames.get(execution.agent_id) || `Agent ${execution.agent_id}`;
    const heading = el("div", { class: "runtime-execution-heading" }, el("div", {}, el("h3", {}, identity),
      el("p", { class: "small muted" }, `${execution.provider} · ${execution.model}`)), badge(label, color));
    const facts = el("dl", { class: "runtime-facts" },
      el("dt", {}, "Ticket"), el("dd", {}, workLink(`Ticket ${String(execution.ticket_id).slice(0, 12)}`, { ticket: execution.ticket_id })),
      el("dt", {}, "Last heartbeat"), el("dd", {}, execution.last_heartbeat_at ? date(execution.last_heartbeat_at) : "Not reported"),
      el("dt", {}, execution.finished_at ? "Finished" : "Reserved"), el("dd", {}, date(execution.finished_at || execution.created_at) || "Not reported"));
    const card = el("article", { class: "runtime-execution", "data-execution-id": execution.id }, heading, facts);
    if (stopping && execution.state !== "needs_review") card.append(el("p", { class: "runtime-control callout" }, el("strong", {}, stopLabel), " · Waiting for the executor to confirm cleanup. Capacity is still reserved."));
    if (active && execution.stale === true) card.append(el("p", { class: "runtime-presence small" }, "Presence stale — no recent heartbeat. This does not confirm that the process has stopped."));
    if (execution.state === "needs_review") card.append(el("p", { class: "small runtime-presence" }, "Cleanup is unconfirmed. Inspect the executor before recovering this work; its capacity remains reserved."));
    if (execution.state === "finished" && execution.cleanup_confirmed === true) card.append(el("p", { class: "small muted" }, "Executor cleanup confirmed. Capacity released."));
    const reason = runtimeReasons[execution.reason];
    if (reason && !["cleanup_unconfirmed", "stop_requested"].includes(execution.reason)) card.append(el("p", { class: "small muted" }, reason));
    const error = el("p", { class: "form-error", role: "alert", hidden: true });
    if (active) {
      const stop = btn(stopping ? stopLabel : "Request stop", async () => {
        stop.disabled = true; error.hidden = true;
        try {
          await api(`/runtime/executions/${workPath(execution.id)}/stop`, { method: "POST", data: { expected_revision: execution.revision } });
          if (state.view === "agents" && agentTab === "runtime" && card.isConnected) await agents();
        } catch (failure) {
          error.textContent = failure.status === 409 ? "This execution changed. Refresh Runtime before requesting a stop again." : failure.message;
          error.hidden = false;
        } finally { stop.disabled = stopping; }
      }, "quiet", { disabled: stopping });
      card.append(el("div", { class: "runtime-execution-actions" }, stop), error);
    }
    return card;
  }
  function runtimeUsageWindow(window, label) {
    const valid = Number.isInteger(window?.used_percent) && window.used_percent >= 0 && window.used_percent <= 100;
    const duration = Number.isInteger(window?.window_minutes) && window.window_minutes > 0
      ? window.window_minutes % 1440 === 0 ? `${window.window_minutes / 1440} days`
        : window.window_minutes % 60 === 0 ? `${window.window_minutes / 60} hours` : `${window.window_minutes} minutes` : null;
    const reset = Number.isSafeInteger(window?.resets_at) && window.resets_at > 0 ? window.resets_at : null;
    return el("div", { class: "runtime-usage-window" }, el("h5", {}, `${label}${duration ? ` · ${duration}` : ""}`),
      el("p", { class: "runtime-remaining" }, valid ? `${100 - window.used_percent}% remaining` : "Remaining usage unavailable"),
      el("p", { class: "small muted" }, reset ? `Reported reset: ${date(reset)}${reset <= Date.now() / 1000 ? " (past)" : ""}` : "Reset not reported"));
  }
  function runtimeUsageCard(observation, agentNames) {
    const snapshot = observation.snapshot || {}, windows = Array.isArray(snapshot.buckets) ? snapshot.buckets : [];
    const observed = Number.isFinite(observation.observed_at) && observation.observed_at > 0 ? observation.observed_at : null;
    const expires = Number.isFinite(observation.expires_at) && observation.expires_at > 0 ? observation.expires_at : null;
    const unavailable = !["observed", "stale"].includes(observation.data_status) || snapshot.data_status !== "observed" || !observed || !expires;
    const stale = !unavailable && (observation.data_status === "stale" || expires <= Date.now() / 1000);
    const identity = observation.agent_name || agentNames.get(observation.agent_id) || `Agent ${observation.agent_id}`;
    const device = observation.device_label || `Device ${String(observation.device_id).slice(0, 12)}`;
    const status = badge(unavailable ? "Unavailable" : stale ? "Stale" : "Observed", stale ? "status-blocked" : "");
    const notice = el("p", { class: "small runtime-usage-notice" }, unavailable
      ? "No usage reading is available for this connection. Missing values are unknown."
      : stale ? "This observation has expired. The values below describe its last reading."
        : "This is a dated local observation, not a guarantee of availability for the next turn.");
    const card = el("article", { class: `runtime-usage${stale ? " usage-stale" : ""}`, "data-connection-id": observation.connection_id },
      el("div", { class: "runtime-execution-heading" }, el("div", {}, el("h3", {}, identity), el("p", { class: "small muted" }, device)), status),
      el("p", { class: "small muted runtime-usage-source" }, "Codex · local client observation"),
      el("p", { class: "small runtime-usage-date" }, observed ? `Observed ${date(observed)}` : "Observation time unavailable"),
      !unavailable && expires ? el("p", { class: "small muted" }, `Expires ${date(expires)}`) : null, notice);
    if (unavailable) {
      const unavailableReasons = { auth_unavailable: "Personal connection unavailable", unsupported_auth: "This connection type has no usage reading",
        transport_error: "Local observation failed", invalid_response: "Local reading could not be interpreted",
        cleanup_unconfirmed: "Local observation cleanup is unconfirmed", process_exited: "Local client stopped before returning a reading",
        unsupported_cli: "Local client does not support this reading", timeout: "Local observation timed out" };
      card.append(el("p", { class: "small muted" }, unavailableReasons[snapshot.unavailable_reason] || "The local client did not provide a usable reading."));
    } else {
      if (typeof snapshot.ordinary_usage_allowed === "boolean") card.append(el("p", { class: "small muted" },
        `At observation, the provider reported ordinary usage ${snapshot.ordinary_usage_allowed ? "allowed" : "unavailable"}.`));
      card.append(windows.length ? el("div", { class: "runtime-usage-buckets" }, windows.map(bucket => {
        const item = el("section", { class: "runtime-usage-bucket" }, el("h4", {}, bucket.limit_id || "Default limit"),
          el("p", { class: "small muted" }, bucket.source_view === "legacy" ? "Legacy provider snapshot" : "Provider bucket"),
          el("div", { class: "runtime-usage-windows" }, runtimeUsageWindow(bucket.primary, "Primary"), runtimeUsageWindow(bucket.secondary, "Secondary")));
        if (bucket.normal_model_slug) item.append(el("p", { class: "small muted" }, `Model reported: ${bucket.normal_model_slug}`));
        if (bucket.reached_reason) item.append(el("p", { class: "small runtime-presence" }, bucket.reached_reason === "rate_limit_reached"
          ? "Provider reported a usage limit." : "Provider reported a workspace usage limit."));
        if (bucket.spend_control_reached === true) item.append(el("p", { class: "small runtime-presence" }, "Provider reported spending control reached."));
        return item;
      })) : el("p", { class: "small muted" }, "No usage windows were supplied."));
      // Expiry changes only this local label. It does not fetch, change limits,
      // or wake an executor; refresh remains an explicit user action.
      if (!stale) setTimeout(() => {
        if (!card.isConnected) return;
        status.textContent = "Stale"; status.classList.add("status-blocked"); card.classList.add("usage-stale");
        notice.textContent = "This observation has expired. The values below describe its last reading.";
      }, Math.min(2147483647, Math.max(0, expires * 1000 - Date.now()) + 10));
    }
    return card;
  }
  async function renderRuntime(body, request) {
    const loading = empty("Loading your runtime…"); body.append(loading);
    try {
      const [data, entries] = await Promise.all([api("/runtime"), api("/agents")]);
      if (request !== state.request) return;
      const limits = data?.limits, owner = state.actor.owner_id;
      if (limits?.owner_id !== owner || !Number.isSafeInteger(limits.version) || !Number.isSafeInteger(limits.max_concurrent)
          || limits.max_concurrent < 1 || limits.max_concurrent > 8 || typeof limits.paused !== "boolean"
          || !Number.isSafeInteger(data.active_count) || data.active_count < 0 || !Array.isArray(data.executions)) throw new Error("Runtime data is unavailable. Refresh to try again.");
      const executions = data.executions.filter(item => item.owner_id === owner);
      const names = new Map(entries.filter(item => item.owner_id === owner).map(item => [item.id, item.name]));
      const active = executions.filter(item => item.state !== "finished"), recent = executions.filter(item => item.state === "finished");
      const providerBlock = data.quota?.status === "blocked_observed";
      const observations = Array.isArray(data.usage_observations) ? data.usage_observations.filter(item => item?.owner_id === owner) : [];
      const quotaLabel = providerBlock ? data.quota.reason === "authentication_failed" ? "Authentication failed" : "Quota or rate limit reported"
        : observations.some(item => item.snapshot?.data_status === "observed") ? "See connection observations" : "Unavailable";
      const maximum = field("Maximum concurrent executions", "max_concurrent", { type: "number", value: limits.max_concurrent, required: true });
      Object.assign(maximum.querySelector("input"), { min: "1", max: "8", step: "1" });
      const paused = check("Pause participating executors", "paused", limits.paused);
      const form = el("form", { class: "runtime-limits" }, maximum, paused,
        el("p", { class: "field-hint" }, "Pause blocks new starts and asks active executors to stop. It takes effect when they check the board; cleanup must still be confirmed. Resuming allows new starts and keeps existing stop requests in effect."),
        el("button", { type: "submit", class: "primary" }, "Save runtime limits"));
      const error = el("p", { class: "form-error", role: "alert", hidden: true }); form.prepend(error);
      submit(form, async values => {
        error.hidden = true;
        const maximum = Number(values.get("max_concurrent"));
        if (!Number.isSafeInteger(maximum) || maximum < 1 || maximum > 8) { error.textContent = "Choose a limit from 1 to 8."; error.hidden = false; return; }
        try {
          await api("/runtime/limits", { method: "PATCH", data: { expected_version: limits.version, max_concurrent: maximum, paused: values.has("paused") } });
          if (request === state.request) await agents();
        } catch (failure) {
          error.textContent = failure.status === 409 ? "Your runtime limits changed. Refresh Runtime to load the current settings before saving again." : failure.message;
          error.hidden = false;
        }
      });
      loading.remove();
      body.append(el("div", { class: "runtime-intro" }, el("p", {}, `Runtime for ${ownerName(owner)}`),
          el("p", { class: "small muted" }, "Only executors using the runtime protocol appear here. These controls share your account’s capacity across its profiles and devices; they do not control other processes."),
          el("p", { class: "small muted runtime-snapshot" }, `Snapshot loaded ${date(Date.now() / 1000)}. Use Refresh for current state.`)),
        el("div", { class: "runtime-summary" },
          panel("Shared capacity", el("div", { class: "panel-body" }, el("p", { class: "runtime-capacity" }, el("strong", {}, data.active_count), ` / ${limits.max_concurrent} occupied`),
            badge(limits.paused ? "Paused" : "Accepting executions", limits.paused ? "status-blocked" : "status-ready"), el("p", { class: "small muted" }, "Reservations awaiting cleanup still occupy capacity."))),
          panel("Provider usage", el("div", { class: "panel-body" }, el("p", { class: "runtime-quota" }, quotaLabel),
            el("p", { class: "small muted" }, providerBlock ? "An executor reported this block. It is not a quota measurement and takes priority over any usage observation."
              : observations.length ? "Each connection has its own dated observation. Connections may use different subscriptions; their percentages are never added together."
                : "No connection has supplied a usage observation. Remaining usage and reset time are unknown."),
            providerBlock ? el("p", { class: "small runtime-presence" }, "New executions are paused. Check provider access before resuming in your runtime limits.") : null))),
        panel("Your runtime limits", el("div", { class: "panel-body" }, form)),
        observations.length ? el("section", { class: "runtime-section", "aria-label": "Usage by connection" }, el("h2", {}, "Usage by connection"),
          el("p", { class: "small muted runtime-usage-intro" }, "These readings belong to agent/device connections, not a confirmed shared subscription. They do not grant permission to run, change runtime limits, or resume paused executions."),
          el("div", { class: "runtime-usages" }, observations.map(item => runtimeUsageCard(item, names)))) : null,
        el("section", { class: "runtime-section", "aria-label": "Active executions" }, el("h2", {}, `Active executions (${active.length})`),
          active.length ? el("div", { class: "runtime-executions" }, active.map(item => runtimeCard(item, names))) : box("No active executions", "A participating executor will appear after it reserves work. This page does not launch an agent.")),
        el("section", { class: "runtime-section", "aria-label": "Recent executions" }, el("h2", {}, "Recent executions"),
          recent.length ? el("div", { class: "runtime-executions" }, recent.map(item => runtimeCard(item, names))) : empty("No recently finished executions.")));
    } catch (failure) {
      if (request !== state.request) return;
      loading.replaceWith(el("div", { class: "callout", role: "alert" }, el("strong", {}, "Runtime unavailable"), el("p", {}, failure.message || "Could not load runtime data. Use Refresh to try again.")));
    }
  }
  async function profileEditor(id = null) {
    const [p, projects] = await Promise.all([id ? api(`/agent-profiles/${workPath(id)}`) : Promise.resolve(null), api("/projects?include_archived=true")]);
    if (p && p.owner_id !== state.actor.owner_id) throw new Error("Only the owner can edit this profile.");
    const key = idempotencyKey();
    const policy = p?.policy || { allow_read: false, allow_messages: false, allow_requests: false, actions: [], resources: [], autonomy: "responses" };
    const form = el("form", { class: "work-form profile-form" });
    const preview = el("aside", { class: "profile-preview", "aria-label": "Profile preview" });
    const basics = el("section", { class: "profile-fields" },
      profileField("Profile name", "name", 200, { value: p?.name || "", required: true, placeholder: "Release reviewer" }),
      el("div", { class: "form-row" }, profileField("Avatar text or emoji", "avatar", 32, { value: p?.avatar || "", placeholder: "🔎", hint: "Plain text only. Images and URLs are not used." }), profileField("Role", "role", 200, { value: p?.role || "", placeholder: "Code review" })),
      profileField("Description", "description", 4000, { type: "textarea", rows: 2, value: p?.description || "", placeholder: "What should this agent focus on?" }));
    const appearance = el("div", { class: "profile-editor-top" }, basics, preview);
    const template = select("Start from a template", "template", [["", "Custom profile"], ["development", "Development"], ["review", "Review"], ["research", "Research"]], "");
    if (!p) form.append(template);
    form.append(appearance,
      profileField("Instructions", "instructions", 12000, { type: "textarea", rows: 6, value: p?.instructions || "", hint: "Private to your account and authorized executor. Do not include passwords or connection keys." }),
      el("div", { class: "form-row" }, select("Response language", "language", [["auto", "Follow the task"], ["en", "English"], ["fr", "French"], ["de", "German"], ["nl", "Dutch"]], p?.language || "auto"), profileField("Response style", "response_style", 2000, { type: "textarea", rows: 2, value: p?.response_style || "", placeholder: "Concise, with concrete evidence" })),
      el("fieldset", { class: "profile-section" }, el("legend", {}, "Execution preferences"),
        el("div", { class: "form-row" }, profileField("Preferred provider", "preferred_provider", 100, { value: p?.preferred_provider || "", placeholder: "Leave empty to use the executor default" }), profileField("Preferred model", "preferred_model", 200, { value: p?.preferred_model || "", placeholder: "Leave empty to use the executor default" })),
        el("p", { class: "small muted" }, "These are preferences for a compatible executor. Saving them does not connect a provider, make a model available, or enable automatic fallback.")));
    const projectPicker = el("fieldset", { class: "profile-section" }, el("legend", {}, "Followed projects"), el("p", { class: "small muted" }, "Choose this profile’s work scope. With no project selected, it has no project scope."),
      el("div", { class: "profile-project-picker" }, projects.length ? projects.map(project => check(`${project.name}${project.archived ? " (archived)" : ""}`, "project_ids", p?.project_ids?.includes(project.id), project.id)) : el("p", { class: "muted" }, "Create a project first to give this profile a work scope.")));
    if (p?.project_ids === null) {
      const keep = check("Keep the existing account project scope", "keep_legacy_scope", true);
      const inputs = [...projectPicker.querySelectorAll('input[name="project_ids"]')];
      inputs.forEach(input => input.disabled = true);
      keep.querySelector("input").addEventListener("change", event => { inputs.forEach(input => input.disabled = event.target.checked); });
      projectPicker.insertBefore(keep, projectPicker.querySelector("p"));
      projectPicker.querySelector("p").textContent = "This existing installation follows its account’s project access. Clear the option to replace that scope with selected projects; an empty selection removes project access.";
    }
    const permissions = el("details", { class: "metadata-editor profile-permissions" }, el("summary", {}, "Profile permissions"),
      el("p", { class: "small muted" }, "These limits are combined with your account and device permissions. A profile cannot grant itself additional access."),
      check("Read selected projects and discussions", "allow_read", policy.allow_read), check("Post messages", "allow_messages", policy.allow_messages), check("Create, claim and update work", "allow_requests", policy.allow_requests),
      select("Work selection", "autonomy", [["responses", "Responses only"], ["requested", "Explicitly requested work"], ["queue", "Authorized queue work"]], policy.autonomy),
      el("p", { class: "small muted" }, "Queue permission does not start a worker or listener. An executor must also support and be authorized for that workflow."),
      el("details", { class: "profile-legacy-scopes" }, el("summary", {}, "Legacy action limits"), profileField("Allowed actions (one per line)", "actions", 25700, { type: "textarea", value: policy.actions.join("\n") }), profileField("Allowed resources (one per line)", "resources", 25700, { type: "textarea", value: policy.resources.join("\n") })));
    form.append(projectPicker, permissions,
      el("p", { class: "small muted" }, p ? `Editing configuration v${p.version}. Saved preferences apply to future work; existing work keeps its captured configuration.` : "A new profile has no connection. Choose its project scope and permissions, then connect an executor."),
      profileWorkNotice(p),
      formButtons(p ? "Save profile" : "Create profile"));
    const refreshPreview = () => {
      const values = new FormData(form);
      preview.replaceChildren(el("p", { class: "small muted" }, "Preview"), profileAvatar({ avatar: String(values.get("avatar") || "") }), el("h3", {}, String(values.get("name") || "Your agent")),
        el("p", { class: "small muted" }, ownerName(state.actor.owner_id)), el("p", {}, String(values.get("role") || "General purpose")), el("p", { class: "small profile-description" }, String(values.get("description") || "A clear purpose helps others know when to involve this agent.")));
    };
    template.querySelector("select").addEventListener("change", event => {
      const preset = profileTemplates[event.target.value]; if (!preset) return;
      Object.entries(preset).forEach(([name, value]) => { form.elements.namedItem(name).value = value; });
      refreshPreview();
    });
    form.addEventListener("input", refreshPreview);
    bindForm(form, async values => {
      const fields = Object.fromEntries(["name", "avatar", "description", "role", "instructions", "language", "response_style", "preferred_provider", "preferred_model"].map(name => [name, String(values.get(name) || "")]));
      if (!values.has("keep_legacy_scope")) fields.project_ids = values.getAll("project_ids");
      fields.policy = { allow_read: values.has("allow_read"), allow_messages: values.has("allow_messages"), allow_requests: values.has("allow_requests"), autonomy: values.get("autonomy"), actions: lines(String(values.get("actions") || "")), resources: lines(String(values.get("resources") || "")) };
      if (fields.policy.actions.length > 100 || fields.policy.resources.length > 100) throw new Error("Use at most 100 allowed actions and 100 allowed resources.");
      await api(p ? `/agent-profiles/${workPath(p.id)}` : "/agent-profiles", { method: p ? "PATCH" : "POST", data: { ...fields, ...(p ? { expected_version: p.version } : { idempotency_key: key }) } });
      closeModal(); agentTab = "profiles"; await agents(); notify(p ? "Profile saved for future work." : "Profile created. Connect an executor when you are ready.");
    });
    openModal(p ? "Edit agent profile" : "Create agent profile", form); refreshPreview();
  }
  function duplicateProfile(p) {
    const key = idempotencyKey();
    const form = el("form", {}, profileField("New profile name", "name", 200, { value: `${p.name.slice(0, 193)} (copy)`, required: true }),
      el("p", { class: "muted" }, "Copies the presentation, instructions and preferences. The copy starts paused, with no project scope, permissions or connected executor."), formButtons("Create copy"));
    bindForm(form, async values => {
      await api(`/agent-profiles/${workPath(p.id)}/duplicate`, { method: "POST", data: { expected_version: p.version, name: values.get("name"), idempotency_key: key } });
      closeModal(); await agents(); notify("Paused copy created. Choose its scope and permissions before using it.");
    });
    openModal("Duplicate profile", form);
  }
  function changeProfileStatus(p, next) {
    const label = next === "active" ? "Resume profile" : next === "archived" ? "Archive profile" : p.status === "archived" ? "Restore paused" : "Pause profile";
    const form = el("form", {}, el("p", {}, next === "active" ? "Allow this profile’s next authorized board calls. This does not start an executor or expand its permissions." : "Block this profile’s subsequent board calls. External processes are not stopped and existing work or leases may still need reconciliation."), formButtons(label));
    bindForm(form, async () => {
      await api(`/agent-profiles/${workPath(p.id)}`, { method: "PATCH", data: { expected_version: p.version, status: next } });
      closeModal(); await agents(); notify(`Profile ${profileStates[next].toLowerCase()}.`);
    });
    openModal(label, form);
  }
  async function profileHistory(id, selectedVersion = null) {
    const p = await api(`/agent-profiles/${workPath(id)}`);
    if (p.owner_id !== state.actor.owner_id) throw new Error("Only the owner can read this profile’s history.");
    const version = field("Configuration version", "version", { type: "number", value: selectedVersion || p.version, required: true });
    const input = version.querySelector("input"); input.min = 1; input.max = p.version; input.step = 1;
    const content = el("div", { class: "profile-version-content", "aria-live": "polite" });
    const form = el("form", { class: "profile-version-form" }, el("p", { class: "small muted" }, `Saved configurations are read-only. Current version: ${p.version}.`),
      el("div", { class: "profile-version-controls" }, version, el("button", { type: "submit" }, "View version")), content);
    let sequence = 0;
    const load = async selected => {
      if (!Number.isSafeInteger(selected) || selected < 1 || selected > p.version) throw new Error(`Choose a version from 1 to ${p.version}.`);
      const current = ++sequence;
      const saved = await api(`/agent-profiles/${workPath(id)}/versions/${selected}`);
      if (current !== sequence || !form.isConnected) return;
      content.replaceChildren(el("div", { class: "profile-heading" }, profileAvatar(saved), el("div", { class: "profile-identity" }, el("h3", {}, saved.name), el("p", { class: "small muted" }, `Version ${saved.version} · ${date(saved.updated_at)}`)), profileStatus(saved)),
        Markdown.block(saved.description || "No description"),
        el("dl", { class: "profile-version-facts" }, ...[
          ["Owner", ownerName(saved.owner_id)], ["Role", saved.role || "General purpose"], ["Language", saved.language || "auto"],
          ["Response style", saved.response_style || "Executor default"], ["Preferred provider", saved.preferred_provider || "Executor default"], ["Preferred model", saved.preferred_model || "Executor default"],
          ["Project scope", saved.project_ids === null ? "Existing account project scope" : saved.project_ids.length ? saved.project_ids.join(", ") : "No projects"]
        ].flatMap(([label, value]) => [el("dt", {}, label), el("dd", {}, value)])),
        panel("Private instructions", Markdown.block(saved.instructions || "No additional instructions", "panel-body")),
        panel("Permission limits", el("div", { class: "panel-body" }, el("p", {}, `Read: ${saved.policy.allow_read ? "allowed" : "off"} · Messages: ${saved.policy.allow_messages ? "allowed" : "off"} · Work requests: ${saved.policy.allow_requests ? "allowed" : "off"}`),
          el("p", { class: "small muted" }, `Work selection: ${saved.policy.autonomy}`),
          el("p", { class: "prose small" }, `Actions: ${saved.policy.actions.join(", ") || "None"}`), el("p", { class: "prose small" }, `Resources: ${saved.policy.resources.join(", ") || "None"}`))));
    };
    bindForm(form, async values => load(Number(values.get("version"))));
    openModal(`Version history · ${p.name}`, form);
    await load(selectedVersion || p.version);
  }
  async function profileBindings(id) {
    const [p, settings] = await Promise.all([api(`/agent-profiles/${workPath(id)}`), api("/settings")]);
    if (p.owner_id !== state.actor.owner_id) throw new Error("Only the owner can connect this profile.");
    const devices = settings.devices.filter(device => !device.revoked);
    const list = el("div", { class: "profile-binding-list" }, (p.bindings || []).map(binding => el("section", { class: "profile-binding" },
      el("strong", {}, binding.device_label), badge(binding.revoked ? "Device revoked" : "Linked"),
      el("p", { class: "small muted" }, binding.agent_kind), el("code", {}, binding.identity_key))));
    const form = el("form", { class: "profile-binding-form" },
      el("p", {}, "Link this profile to a client on an existing connected device. Credentials remain on that device."), list);
    if (!devices.length) {
      form.append(el("p", { class: "muted" }, "Connect a device first, then return here to link its executor."), btn("Connect a device", guard(async () => { closeModal(); await setup(); }), "primary"));
    } else {
      form.append(select("Connected device", "device_id", devices.map(device => [device.id, device.label]), devices[0].id),
        select("Client", "agent_kind", [["codex", "Codex"], ["claude-code", "Claude Code"], ["other", "Other MCP client"]], "codex"),
        profileField("Installation key", "identity_key", 200, { value: `profile-${p.id}`, required: true, hint: "Use this key with the connector on the chosen device. A key already linked to another profile cannot be moved." }),
        el("p", { class: "small muted" }, `Configuration v${p.version}. Linking an executor does not change this profile’s permissions.`), profileWorkNotice(p), formButtons("Link executor"));
      bindForm(form, async values => {
        const binding = { device_id: values.get("device_id"), agent_kind: values.get("agent_kind"), identity_key: values.get("identity_key") };
        await api(`/agent-profiles/${workPath(p.id)}/bindings`, { method: "POST", data: { expected_version: p.version, ...binding } });
        const quote = value => `'${String(value).replaceAll("'", "'\"'\"'")}'`;
        const command = `python3 ~/Downloads/board-connect.py --client ${quote(binding.agent_kind)} --select-agent-key ${quote(binding.identity_key)}`;
        openModal("Executor linked", el("div", { class: "profile-binding-success" }, el("p", {}, "On the selected device, use its existing connection with this installation key:"),
          el("a", { href: basePath + "connect.py", download: "board-connect.py", class: "button-link" }, "Download current connector"),
          el("pre", {}, command), el("p", { class: "small muted" }, "The connector selects this profile using the device’s existing credential. This command does not create a new provider or start work."),
          el("p", { class: "small muted" }, "Restart the client or open a new conversation after selecting the profile. Existing work is not reassigned. If you use a custom connection folder, add --config-dir with that folder."),
          p.status !== "active" ? el("p", { class: "callout" }, "This profile is paused or archived. Resume it when its scope and permissions are ready.") : null,
          btn("Done", guard(async () => { closeModal(); await agents(); }), "primary")));
      });
    }
    openModal(`Connections · ${p.name}`, form);
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
    const loopPrompt = "Start a complete autonomous loop on the blackoutburst board. Take on task and work on them. Make the loop check your personal inbox every 10 minutes. Nobody will read this conversation if you need something or help use the MCP to ask a human or other agents.";
    const loopText = el("pre", { class: "prose", tabindex: "0", "aria-label": "Autonomous loop prompt" }, loopPrompt);
    const copyLoop = btn("Copy loop prompt", async () => {
      try { await navigator.clipboard.writeText(loopPrompt); notify("Autonomous loop prompt copied."); }
      catch {
        const selection = window.getSelection(), range = document.createRange();
        range.selectNodeContents(loopText); selection.removeAllRanges(); selection.addRange(range);
        loopText.focus(); notify("Select and copy the autonomous loop prompt from this box.");
      }
    });
    const participation = el("div", { class: "panel-body" },
      el("p", {}, "Restart your client, then tell it to read the board. Choose observer mode for notifications, or explicitly authorize worker mode for a project."),
      el("pre", { class: "prose" }, "Register this chat with the board, read my project work, and tell me what is ready or needs my input."),
      el("h3", {}, "Start an autonomous worker loop"),
      el("p", {}, "To have your connected assistant take eligible tasks and ask for help on the board, paste this prompt into its chat:"), loopText, copyLoop,
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
    const body = frame("Inbox", "Tasks assigned to you and unread messages that mention you.");
    body.append(el("div", {id:"inbox-content"}, empty("Loading…")));
    const data = await api("/inbox");
    if (request === state.request) renderInbox(data);
    await refreshRequestCount();
  }
  function renderInbox(data) {
    const container = $("inbox-content"); if (!container) return;
    const discussions = data.messages.map(message => {
      const item = btn("", guard(() => openThread(message.thread_id)), "inbox-message");
      item.append(authorMeta(message.author, message.created_at), el("p", {class:"prose"}, message.body), badge("Mentioned you", "status-running"));
      return item;
    });
    container.replaceChildren(
      panel("Assigned to you", data.tickets.length ? el("div", {class:"issue-list"}, data.tickets.map(taskRow)) : box("No assigned tasks", "Tasks assigned to you or your agents will appear here.")),
      panel("Unread mentions", discussions.length ? discussions : box("No unread mentions", "You are all caught up.")),
      ...(data.tasks.length ? [panel("Action requests", data.tasks.map(taskCard))] : []));
  }
  $("agents-nav").addEventListener("click", guard(agents));
  $("requests-nav").addEventListener("click", guard(requests));
  $("guide-link").addEventListener("click", event => { event.preventDefault(); guard(setup)(); });
  return { projects, project, ticket, renderTicket, refreshTicket, renderInbox, projectEditor, ticketEditor, agents, requests, setup, settings, inbox, refreshRequestCount };
})();
