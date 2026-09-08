// 投递管理：公司记录、阶段步骤条、时间线与添加/编辑表单
import { request } from "./api.js";
import { text } from "./dom.js";
import { cleanApplyUrl, shortDate } from "./format.js";
import { APP_CLOSED, APP_CLOSED_KEYS, APP_STAGES, APP_STATUS_MAP, state } from "./state.js";
import { registerView, rerenderViews, switchView, toast, updateTabCounts } from "./views.js";

export function statusLabel(key) {
  return APP_STATUS_MAP[key] || key;
}

function eventLabel(history, index) {
  if (history[index].key === "test") {
    const round = history.slice(0, index + 1).filter((event) => event.key === "test").length;
    return `第${round}轮笔试`;
  }
  return statusLabel(history[index].key);
}

export function currentStatusLabel(app) {
  if (!app.history.length) return statusLabel("planned");
  return eventLabel(app.history, app.history.length - 1);
}

function appIsClosed(app) {
  return APP_CLOSED_KEYS.has(app.status);
}

export function sortApplications() {
  state.applications.sort((a, b) => b.updated_at.localeCompare(a.updated_at) || b.id - a.id);
}

export function rebuildAppliedIds() {
  state.appliedGroupIds = new Set(
    state.applications.map((app) => app.article_group_id).filter(Boolean),
  );
}

export async function loadApplications() {
  state.applications = (await request("/api/applications")).applications;
  sortApplications();
  rebuildAppliedIds();
}

function replaceApplication(next) {
  state.applications = state.applications.map((app) => (app.id === next.id ? next : app));
  sortApplications();
  rebuildAppliedIds();
}

async function updateApplication(body) {
  const payload = await request("/api/applications/update", { method: "POST", body: JSON.stringify(body) });
  replaceApplication(payload.application);
  renderApplicationsView();
}

// 公司名优先用 AI 筛选抽取的「招聘单位」，没有时回落到文章标题（表单里可改）
export function companyFromArticle(article) {
  const unit = (article.screen?.unit || "").trim();
  return unit || article.title;
}

export function openApplicationForm(prefill = {}, returnTo = null) {
  state.appReturnView = returnTo && returnTo !== "applications" ? returnTo : null;
  state.appFormArticle = prefill.article_group_id
    ? { article_group_id: prefill.article_group_id, article_url: prefill.article_url || "", article_title: prefill.article_title || "" }
    : null;
  document.querySelector("#app-company").value = prefill.company || "";
  document.querySelector("#app-joburl").value = prefill.job_url || "";
  document.querySelector("#app-note").value = "";
  setAppFormStatus(prefill.status || "applied");
  const articleNote = document.querySelector("#app-form-article");
  if (state.appFormArticle) {
    articleNote.hidden = false;
    articleNote.textContent = `来源文章：${state.appFormArticle.article_title}`;
  } else {
    articleNote.hidden = true;
  }
  document.querySelector("#app-form-error").textContent = "";
  document.querySelector("#app-add-form").hidden = false;
  if (returnTo) switchView("applications");
  document.querySelector("#app-company").focus();
}

export function closeApplicationForm() {
  document.querySelector("#app-add-form").hidden = true;
  state.appFormArticle = null;
  state.appReturnView = null;
}

function setAppFormStatus(key) {
  state.appFormStatus = key;
  document.querySelectorAll("#app-form-status [data-form-status]").forEach((button) => {
    button.classList.toggle("active", button.dataset.formStatus === key);
  });
}

export function prefillFromArticle(group) {
  openApplicationForm({
    company: companyFromArticle(group),
    job_url: cleanApplyUrl(group.screen?.apply_url) || "",
    article_group_id: group.id,
    article_url: group.url,
    article_title: group.title,
    status: "applied",
  }, "articles");
}

export function renderApplicationsView() {
  const chips = document.querySelector("#app-filter-chips");
  const activeCount = state.applications.filter((app) => !appIsClosed(app)).length;
  chips.replaceChildren(
    ...[["all", "全部", state.applications.length], ["active", "进行中", activeCount], ["closed", "已结束", state.applications.length - activeCount]]
      .map(([value, label, count]) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = `chip${state.appFilter === value ? " active" : ""}`;
        chip.append(text("span", "", label));
        if (count) chip.append(text("span", "chip-count", String(count)));
        chip.addEventListener("click", () => {
          state.appFilter = value;
          renderApplicationsView();
        });
        return chip;
      }),
  );

  const query = state.appQuery;
  const visible = state.applications.filter((app) => {
    if (state.appFilter === "active" && appIsClosed(app)) return false;
    if (state.appFilter === "closed" && !appIsClosed(app)) return false;
    if (query) {
      const haystack = [app.company, app.note, app.article_title].join(" ").toLocaleLowerCase("zh-CN");
      if (!haystack.includes(query)) return false;
    }
    return true;
  });
  const list = document.querySelector("#app-list");
  list.replaceChildren();
  for (const app of visible) list.append(createAppCard(app));
  document.querySelector("#app-empty").hidden = visible.length > 0;
}

function createAppCard(app) {
  const card = document.createElement("article");
  card.className = "app-card";

  const head = document.createElement("div");
  head.className = "app-head";
  head.append(text("strong", "app-company", app.company));
  head.append(text("span", `app-badge${appIsClosed(app) ? " closed" : ""}`, currentStatusLabel(app)));
  const links = document.createElement("div");
  links.className = "app-links";
  if (app.job_url) {
    const job = document.createElement("a");
    job.href = app.job_url;
    job.target = "_blank";
    job.rel = "noopener";
    job.textContent = app.job_url.startsWith("mailto:") ? "邮件联系 ↗" : "招聘链接 ↗";
    links.append(job);
  }
  if (app.article_url) {
    const article = document.createElement("a");
    article.href = app.article_url;
    article.target = "_blank";
    article.rel = "noopener";
    article.textContent = "公众号文章 ↗";
    links.append(article);
  }
  if (links.children.length) head.append(links);
  card.append(head);

  // 阶段步骤条：点某个阶段即推进到该阶段（点「笔试」可重复记轮次），右侧是终止状态
  const stepper = document.createElement("div");
  stepper.className = "app-stepper";
  const stageClick = (key) => () => {
    updateApplication({ id: app.id, status: key }).catch((error) => toast(`更新失败：${error.message}`, true));
  };
  for (const stage of APP_STAGES) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `step${app.status === stage.key ? " active" : ""}`;
    chip.textContent = stage.label;
    if (stage.key === "test" && app.status === "test") chip.title = "再点一次记下一轮笔试";
    chip.addEventListener("click", stageClick(stage.key));
    stepper.append(chip);
  }
  stepper.append(text("span", "step-divider", ""));
  for (const closed of APP_CLOSED) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `step closed${app.status === closed.key ? " active" : ""}`;
    chip.textContent = closed.label;
    chip.addEventListener("click", stageClick(closed.key));
    stepper.append(chip);
  }
  card.append(stepper);

  if (app.history.length) {
    const timeline = document.createElement("div");
    timeline.className = "app-timeline";
    app.history.forEach((event, index) => {
      const label = `${shortDate(event.at)} ${eventLabel(app.history, index)}${event.note ? `（${event.note}）` : ""}`;
      timeline.append(text("span", "tl-item", label));
    });
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "tl-undo";
    undo.textContent = "撤销";
    undo.title = "删掉最后一条进度";
    undo.addEventListener("click", () => {
      updateApplication({ id: app.id, undo: true }).catch((error) => toast(`撤销失败：${error.message}`, true));
    });
    timeline.append(undo);
    card.append(timeline);
  }

  if (app.note) card.append(text("p", "app-note", app.note));

  const actions = document.createElement("div");
  actions.className = "app-actions";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = "编辑";
  edit.addEventListener("click", () => openAppEditor(card, app));
  const del = document.createElement("button");
  del.type = "button";
  del.textContent = "删除";
  del.addEventListener("click", async () => {
    if (!window.confirm(`删除「${app.company}」的投递记录？`)) return;
    try {
      await request("/api/applications/delete", { method: "POST", body: JSON.stringify({ id: app.id }) });
      state.applications = state.applications.filter((item) => item.id !== app.id);
      rebuildAppliedIds();
      rerenderViews();
    } catch (error) {
      toast(`删除失败：${error.message}`, true);
    }
  });
  actions.append(edit, del);
  card.append(actions);
  return card;
}

function openAppEditor(card, app) {
  const form = document.createElement("form");
  form.className = "app-edit";
  const fields = [
    ["公司名称", app.company, "app-edit-company"],
    ["招聘链接", app.job_url, "app-edit-joburl"],
    ["备注", app.note, "app-edit-note"],
  ];
  const inputs = {};
  for (const [label, value, key] of fields) {
    const wrap = document.createElement("label");
    wrap.append(text("span", "", label));
    const input = document.createElement("input");
    input.value = value;
    wrap.append(input);
    inputs[key] = input;
    form.append(wrap);
  }
  const actions = document.createElement("div");
  actions.className = "app-edit-actions";
  const save = document.createElement("button");
  save.type = "submit";
  save.textContent = "保存";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "secondary-btn";
  cancel.textContent = "取消";
  cancel.addEventListener("click", renderApplicationsView);
  actions.append(save, cancel);
  form.append(actions);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = await request("/api/applications/update", {
        method: "POST",
        body: JSON.stringify({
          id: app.id,
          company: inputs["app-edit-company"].value,
          job_url: inputs["app-edit-joburl"].value,
          note: inputs["app-edit-note"].value,
        }),
      });
      replaceApplication(payload.application);
      renderApplicationsView();
      toast("已保存");
    } catch (error) {
      toast(`保存失败：${error.message}`, true);
    }
  });
  card.replaceChildren(form);
  inputs["app-edit-company"].focus();
}

// ---------- 表单与筛选的交互绑定 ----------

document.querySelector("#app-form-toggle").addEventListener("click", () => {
  if (document.querySelector("#app-add-form").hidden) openApplicationForm({});
  else closeApplicationForm();
});

document.querySelector("#app-form-cancel").addEventListener("click", closeApplicationForm);

document.querySelectorAll("#app-form-status [data-form-status]").forEach((button) => {
  button.addEventListener("click", () => setAppFormStatus(button.dataset.formStatus));
});

document.querySelector("#app-add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorNode = document.querySelector("#app-form-error");
  errorNode.textContent = "";
  const body = {
    company: document.querySelector("#app-company").value.trim(),
    job_url: document.querySelector("#app-joburl").value.trim(),
    note: document.querySelector("#app-note").value.trim(),
    status: state.appFormStatus,
    ...(state.appFormArticle || {}),
  };
  if (!body.company) {
    errorNode.textContent = "请填写公司名称";
    return;
  }
  try {
    const payload = await request("/api/applications", { method: "POST", body: JSON.stringify(body) });
    state.applications = [payload.application, ...state.applications];
    sortApplications();
    rebuildAppliedIds();
    const returnTo = state.appReturnView; // 先取回跳转目标，closeApplicationForm 会清掉它
    closeApplicationForm();
    updateTabCounts();
    toast(`已把「${payload.application.company}」加入投递管理`);
    if (returnTo) switchView(returnTo);
    else renderApplicationsView();
  } catch (error) {
    errorNode.textContent = `添加失败：${error.message}`;
  }
});

document.querySelector("#app-search").addEventListener("input", (event) => {
  state.appQuery = event.target.value.trim().toLocaleLowerCase("zh-CN");
  renderApplicationsView();
});

registerView("applications", renderApplicationsView);
