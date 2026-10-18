(() => {
  const module = document.querySelector(".queue-module");
  const safeArea = document.querySelector(".queue-safe-area");
  const queue = document.querySelector("#queue");
  let lastRevision = null;
  let failedRefreshes = 0;

  const IMG_W = 936;         // 面板基准宽度（用于计算缩放系数 --s）
  const SCROLL_SPEED = 28;   // 滚动速度：像素/秒
  const NAME_MIN_RATIO = 0.55; // 名字最小可缩到基准字号的 55%，再放不下才省略

  function setBackendOnline(online) {
    if (online) {
      failedRefreshes = 0;
      module.classList.remove("backend-offline");
      module.removeAttribute("aria-hidden");
      return;
    }

    failedRefreshes += 1;
    // OBS keeps the last rendered browser frame after the local server exits.
    // Two failed heartbeats mean the backend is gone, so erase that frame.
    if (failedRefreshes >= 2) {
      lastRevision = null;
      queue.replaceChildren();
      module.classList.add("backend-offline");
      module.setAttribute("aria-hidden", "true");
    }
  }

  function layout() {
    const scale = module.offsetWidth / IMG_W;
    module.style.setProperty("--s", String(scale));
  }

  // 尽量显示完整名字：放不下时按行缩小字号，缩小到下限仍放不下才交给 CSS 省略号
  function fitNames() {
    document.querySelectorAll(".queue-row").forEach((row) => {
      const name = row.querySelector(".name");
      if (!name) return;
      name.style.fontSize = "";
      if (name.scrollWidth <= name.clientWidth + 0.5) return; // 本来就放得下
      const base = parseFloat(getComputedStyle(row).fontSize) || 24;
      const min = Math.max(10, base * NAME_MIN_RATIO);
      let low = min, high = base, fit = null;
      for (let i = 0; i < 8; ++i) {
        const mid = (low + high) / 2;
        name.style.fontSize = `${mid.toFixed(2)}px`;
        if (name.scrollWidth <= name.clientWidth + 0.5) { fit = mid; high = mid; }
        else low = mid;
      }
      if (fit === null) name.style.fontSize = `${min.toFixed(2)}px`; // 最小字号仍放不下 → 省略
    });
  }

  // 面板尺寸固定，放不下时把名单复制一份向上循环滚动
  function updateScrolling() {
    queue.querySelectorAll("[data-clone]").forEach((node) => node.remove());
    queue.classList.remove("scrolling");

    const rows = [...queue.children];
    if (!rows.length) return;
    const areaHeight = safeArea.clientHeight;
    const contentHeight = queue.scrollHeight;
    if (contentHeight <= areaHeight + 1) return;

    rows.forEach((row) => {
      const copy = row.cloneNode(true);
      copy.dataset.clone = "1";
      queue.appendChild(copy);
    });

    const firstCopy = queue.children[rows.length];
    const distance = firstCopy.getBoundingClientRect().top - rows[0].getBoundingClientRect().top;
    const duration = Math.max(6, distance / SCROLL_SPEED);
    queue.style.setProperty("--scroll-distance", `${Math.round(distance)}px`);
    queue.style.setProperty("--scroll-duration", `${duration.toFixed(2)}s`);
    queue.classList.add("scrolling");
  }

  function render(state) {
    document.documentElement.style.setProperty("--font-size", `${state.font_size}px`);
    // 面板整体透明度与名单透明度分开调节，分别由管理页两个滑动条控制
    document.documentElement.style.setProperty("--panel-opacity", (state.frame_opacity ?? 100) / 100);
    document.documentElement.style.setProperty("--name-opacity", (state.name_opacity ?? 100) / 100);
    // Polling returns the same state every 500ms. Rebuilding unchanged rows would
    // restart their entrance animation and make names bounce continuously.
    if (state.revision !== lastRevision) {
      lastRevision = state.revision;
      const countEl = document.getElementById("queue-count");
      if (countEl) countEl.textContent = state.queue.length;
      const maxRows = Number(state.max_rows || 50);
      queue.replaceChildren(...state.queue.slice(0, maxRows).map((item) => {
        const row = document.createElement("div");
        row.className = "queue-row";
        row.dataset.id = item.id;

        const rank = document.createElement("span");
        rank.className = "rank";
        rank.textContent = item.position;
        const arrow = document.createElement("span");
        arrow.className = "arrow";
        arrow.textContent = "→";
        const name = document.createElement("span");
        name.className = "name";
        name.textContent = item.name;
        row.append(rank, arrow, name);

        if (item.content) { // 「排队，轨外」这类带内容的入队
          const tag = document.createElement("span");
          tag.className = "content";
          tag.textContent = item.content;
          row.append(tag);
        }
        return row;
      }));
      fitNames();
      updateScrolling();
    }
  }

  async function refresh() {
    try {
      const response = await fetch("/api/live-queue/state", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      setBackendOnline(true);
      render(state);
    } catch (_) {
      setBackendOnline(false);
    }
  }

  window.addEventListener("resize", () => {
    layout();
    fitNames();
    updateScrolling();
  });

  layout();
  refresh();
  setInterval(refresh, 500);
})();
