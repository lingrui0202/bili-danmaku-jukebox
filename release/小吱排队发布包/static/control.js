(() => {
  const $ = (selector) => document.querySelector(selector);
  const list = $("#queueList");
  const resultBox = $("#result");
  const commandInput = $("#command");
  let latestState = null;
  let fontTimer = null;
  let frameTimer = null;
  let nameTimer = null;
  let priorityLocked = false; // 开关切换后等待后端确认，避免轮询把开关弹回去
  let autoclearLocked = false;

  function safeText(value) { return document.createTextNode(String(value)); }

  function render(state) {
    latestState = state;
    qrRefresh();
    $("#roomId").textContent = state.room_id;
    $("#queueCount").textContent = state.queue.length;
    $("#pendingCount").textContent = state.pending_priority.length;
    $("#servedCount").textContent = state.served_count ?? 0;
    $("#keyword").textContent = (state.keywords && state.keywords.length)
      ? state.keywords.join(" / ") : state.keyword;
    $("#fontSize").value = state.font_size;
    $("#fontOutput").textContent = `${state.font_size}px`;
    $("#frameOpacity").value = state.frame_opacity;
    $("#frameOpacityOutput").textContent = `${state.frame_opacity}%`;
    $("#nameOpacity").value = state.name_opacity;
    $("#nameOpacityOutput").textContent = `${state.name_opacity}%`;
    $("#overlayUrl").textContent = `${location.origin}/console`;
    // 开关语义：勾选 = 取消插队（后端 priority_enabled 为 false）
    if (!priorityLocked) $("#priorityToggle").checked = !state.priority_enabled;
    if (!autoclearLocked) $("#autoclearToggle").checked = !!state.clear_on_start;
    // 输入框获得焦点时不覆盖用户正在编辑的内容
    if (document.activeElement !== $("#contentsInput"))
      $("#contentsInput").value = (state.contents || []).join("，");
    $("#giftName").textContent = (state.priority_gifts && state.priority_gifts[0]) || "粉丝团灯牌";

    const connection = $("#connection");
    connection.className = `connection ${state.live_status.state}`;
    connection.querySelector("span").textContent = state.live_status.message;

    if (!state.queue.length) {
      list.innerHTML = '<p class="empty">暂无排队用户</p>';
    } else {
      list.replaceChildren(...state.queue.map((item) => {
        const row = document.createElement("div");
        row.className = "queue-item";
        const rank = document.createElement("span");
        rank.className = "queue-rank";
        rank.textContent = String(item.position).padStart(2, "0");
        const info = document.createElement("div");
        const name = document.createElement("div");
        name.className = "queue-name";
        name.append(safeText(item.name));
        const meta = document.createElement("div");
        meta.className = "queue-meta";
        meta.textContent = item.priority ? "灯牌优先" : (item.source === "manual" ? "手动加入" : "弹幕加入");
        if (item.content) meta.textContent += ` · 要打：${item.content}`;
        info.append(name, meta);
        const remove = document.createElement("button");
        remove.className = "remove";
        remove.type = "button";
        remove.title = `删除第 ${item.position} 位`;
        remove.textContent = "×";
        remove.addEventListener("click", () => runCommand(`del-${item.position}`));
        row.append(rank, info, remove);
        return row;
      }));
    }

    const pending = $("#pendingList");
    if (!state.pending_priority.length) {
      pending.innerHTML = '<p class="empty small">暂无记录</p>';
    } else {
      pending.replaceChildren(...state.pending_priority.map((item) => {
        const row = document.createElement("div");
        row.className = "pending-user";
        const name = document.createElement("span");
        name.textContent = item.name;
        const credits = document.createElement("span");
        credits.textContent = `${item.credits} 次优先`;
        row.append(name, credits);
        return row;
      }));
    }
  }

  function showResult(ok, message) {
    resultBox.className = `result ${ok ? "ok" : "error"}`;
    resultBox.textContent = message;
  }

  async function runCommand(command) {
    if (!command.trim()) return;
    try {
      const response = await fetch("/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command }),
      });
      const payload = await response.json();
      showResult(payload.ok, payload.message);
      if (!payload.changed) await refresh();
    } catch (error) {
      showResult(false, `连接失败：${error.message}`);
    }
  }

  async function refresh() {
    try {
      const response = await fetch("/api/state");
      render(await response.json());
    } catch (error) {
      showResult(false, `刷新失败：${error.message}`);
    }
  }

  $("#commandForm").addEventListener("submit", (event) => {
    event.preventDefault();
    const command = commandInput.value.trim();
    runCommand(command);
    commandInput.select();
  });

  document.querySelectorAll("[data-fill]").forEach((button) => {
    button.addEventListener("click", () => {
      commandInput.value = button.dataset.fill;
      commandInput.focus();
      commandInput.setSelectionRange(commandInput.value.length, commandInput.value.length);
    });
  });
  document.querySelectorAll("[data-command]").forEach((button) => {
    button.addEventListener("click", () => {
      if (button.dataset.command === "clear" && latestState?.queue.length && !confirm("确定清空整个队列吗？")) return;
      runCommand(button.dataset.command);
    });
  });
  $("#refresh").addEventListener("click", refresh);
  $("#testQueue").addEventListener("click", () => runCommand(`test-queue-${$("#testName").value.trim()}`));
  $("#testGift").addEventListener("click", () => runCommand(`test-gift-${$("#testName").value.trim()}`));
  $("#contentsApply").addEventListener("click", async () => {
    const value = $("#contentsInput").value.trim();
    if (!value) { showResult(false, "请填写至少一个内容关键词"); return; }
    await runCommand(`contents-${value}`);
    await refresh();
  });
  $("#contentsInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); $("#contentsApply").click(); }
  });
  /* ===== B 站扫码登录 ===== */
  let qrRenderedUrl = "";
  const QR_HINTS = { 1: "等待扫码…", 2: "已扫码，请在手机上确认", 3: "登录成功", 4: "二维码已失效，请重新生成" };

  function qrRender(url) {
    const box = $("#qrCanvas");
    box.innerHTML = "";
    if (!url) { box.classList.remove("active"); qrRenderedUrl = ""; return; }
    new QRCode(box, {
      text: url, width: 168, height: 168,
      colorDark: "#2b0f22", colorLight: "#ffffff",
      correctLevel: QRCode.CorrectLevel.M,
    });
    box.classList.add("active");
    qrRenderedUrl = url;
  }

  async function qrRefresh() {
    let state;
    try {
      state = await (await fetch("/api/qr", { cache: "no-store" })).json();
    } catch (_) { return; }
    const badge = $("#loginState");
    badge.textContent = state.logged_in ? "已登录" : "匿名";
    badge.className = `login-state${state.logged_in ? " on" : ""}`;
    if (state.logged_in) {
      $("#qrHint").textContent = state.user ? `已登录：${state.user}` : "已登录，完整昵称可用";
      qrRender("");
    } else if (state.url) {
      if (state.url !== qrRenderedUrl) qrRender(state.url);
      $("#qrHint").textContent = state.message || QR_HINTS[state.state] || "等待扫码…";
    } else {
      qrRender("");
      $("#qrHint").textContent = "匿名连接时 B 站可能只给脱敏昵称（例如「你***」）。扫码登录后可获取完整用户名。";
    }
  }

  async function qrAction(action, message) {
    try {
      const response = await fetch("/api/qr", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const payload = await response.json();
      showResult(payload.ok, payload.message);
      qrRenderedUrl = "";
      await qrRefresh();
    } catch (error) {
      showResult(false, `操作失败：${error.message}`);
    }
  }

  $("#qrBegin").addEventListener("click", () => qrAction("begin"));
  $("#qrLogout").addEventListener("click", async () => {
    if (!confirm("确定退出 B 站登录，回到匿名模式？")) return;
    await qrAction("logout");
  });

  $("#nextUser").addEventListener("click", async () => {
    await runCommand("next");
    await refresh();
  });
  $("#autoclearToggle").addEventListener("change", async (event) => {
    autoclearLocked = true;
    await runCommand(event.target.checked ? "autoclear-on" : "autoclear-off");
    autoclearLocked = false;
    await refresh();
  });
  $("#roomApply").addEventListener("click", async () => {
    const value = $("#roomInput").value.trim();
    if (!value) { showResult(false, "请先填写直播间号"); return; }
    await runCommand(`room-${value}`);
    $("#roomInput").value = "";
  });
  $("#roomInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); $("#roomApply").click(); }
  });
  $("#priorityToggle").addEventListener("change", async (event) => {
    priorityLocked = true;
    await runCommand(event.target.checked ? "priority-off" : "priority-on");
    priorityLocked = false;
    await refresh();
  });
  $("#fontSize").addEventListener("input", (event) => {
    const size = event.target.value;
    $("#fontOutput").textContent = `${size}px`;
    clearTimeout(fontTimer);
    fontTimer = setTimeout(() => runCommand(`font-${size}`), 110);
  });
  $("#frameOpacity").addEventListener("input", (event) => {
    const op = event.target.value;
    $("#frameOpacityOutput").textContent = `${op}%`;
    clearTimeout(frameTimer);
    frameTimer = setTimeout(() => runCommand(`frame-${op}`), 110);
  });
  $("#nameOpacity").addEventListener("input", (event) => {
    const op = event.target.value;
    $("#nameOpacityOutput").textContent = `${op}%`;
    clearTimeout(nameTimer);
    nameTimer = setTimeout(() => runCommand(`name-${op}`), 110);
  });

  refresh();
  setInterval(refresh, 800);
})();
