/* FMS Portal - shared client-side behaviour (no framework, HTMX + Alpine handle the rest). */

(function () {
  const STORAGE_KEY = "fms-portal-theme";

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(STORAGE_KEY, theme);
  }

  window.toggleTheme = function () {
    const current = document.documentElement.getAttribute("data-theme") || "light";
    applyTheme(current === "dark" ? "light" : "dark");
  };

  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) applyTheme(saved);

  window.toggleSidebar = function () {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;

    if (window.matchMedia("(max-width: 860px)").matches) {
      sidebar.classList.toggle("open");
      return;
    }

    sidebar.classList.toggle("collapsed");
  };

  function initReorderableQuestionGrids() {
    
    // FIX: Agar page Iframe ke andar khula hai (yani Dashboard Modal mein), 
    // toh Dragging ko puri tarah band kar do aur UI clean kar do.
    if (window.self !== window.top) {
        document.querySelectorAll(".question-grid[data-reorderable='true']").forEach(grid => {
            grid.removeAttribute("data-reorderable"); // CSS se ☰ icon hata dega
        });
        document.querySelectorAll("[data-reset-order]").forEach(btn => btn.style.display = 'none');
        document.querySelectorAll(".form-grid-toolbar span").forEach(span => {
            if (span.textContent.includes("Drag cards")) {
                span.style.display = 'none';
            }
        });
        return; // Yahan se function exit ho jayega, drag events attach nahi honge
    }

    // Sirf Full-page (New Tab) par hi dragging allow hogi
    document.querySelectorAll(".question-grid[data-reorderable='true']").forEach((grid) => {
      const cards = Array.from(grid.querySelectorAll(".question[data-draggable='true']"));
      if (!cards.length) return;

      const originalOrder = cards.map((card) => card.dataset.field || "");
      let draggedCard = null;

      const refreshOrder = () => {
        const orderedCards = Array.from(grid.querySelectorAll(".question[data-draggable='true']"));
        orderedCards.forEach((card, index) => {
          card.dataset.order = String(index + 1);
        });
      };

      const restoreOrder = () => {
        const cardMap = new Map(
          Array.from(grid.querySelectorAll(".question[data-draggable='true']")).map((card) => [card.dataset.field || "", card])
        );
        const fragment = document.createDocumentFragment();
        originalOrder.forEach((field) => {
          const card = cardMap.get(field);
          if (card) fragment.appendChild(card);
        });
        grid.appendChild(fragment);
        refreshOrder();
      };

      cards.forEach((card) => {
        card.setAttribute("draggable", "true");

        card.addEventListener("dragstart", (event) => {
          draggedCard = card;
          card.classList.add("question--dragging");
          event.dataTransfer.effectAllowed = "move";
          event.dataTransfer.setData("text/plain", card.dataset.field || "");
        });

        card.addEventListener("dragend", () => {
          draggedCard = null;
          card.classList.remove("question--dragging");
        });

        card.addEventListener("dragover", (event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = "move";
        });

        card.addEventListener("drop", (event) => {
          event.preventDefault();
          if (!draggedCard || draggedCard === card) return;
          grid.insertBefore(draggedCard, card);
          refreshOrder();
        });
      });

      const resetButton = grid.closest("section, .card")?.querySelector("[data-reset-order]");
      resetButton?.addEventListener("click", restoreOrder);
      refreshOrder();
    });
  }

  document.addEventListener("DOMContentLoaded", initReorderableQuestionGrids);
  document.addEventListener("htmx:afterSwap", initReorderableQuestionGrids);

  window.showToast = function (message, timeout = 2200) {
    const toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), timeout);
  };

  window.copyText = async function (text) {
    try {
      await navigator.clipboard.writeText(text);
      window.showToast("Copied to clipboard");
    } catch {
      window.showToast("Copy failed");
    }
  };

  // ---- Sortable / resizable data-table columns (dashboard grid) ----------
  document.addEventListener("click", (event) => {
    const th = event.target.closest("[data-sort-key]");
    if (!th) return;
    const table = th.closest("table");
    const index = Array.from(th.parentNode.children).indexOf(th);
    const tbody = table.querySelector("tbody");
    const rows = Array.from(tbody.querySelectorAll("tr"));
    const asc = th.dataset.sortDir !== "asc";
    table.querySelectorAll("[data-sort-key]").forEach((el) => delete el.dataset.sortDir);
    th.dataset.sortDir = asc ? "asc" : "desc";

    rows.sort((a, b) => {
      const av = a.children[index]?.innerText.trim() ?? "";
      const bv = b.children[index]?.innerText.trim() ?? "";
      const an = parseFloat(av), bn = parseFloat(bv);
      const cmp = !isNaN(an) && !isNaN(bn) ? an - bn : av.localeCompare(bv);
      return asc ? cmp : -cmp;
    });
    rows.forEach((row) => tbody.appendChild(row));
  });

  // Simple column resize handles for the dashboard grid.
  document.addEventListener("mousedown", (event) => {
    const th = event.target.closest("th");
    if (!th || event.offsetX < th.clientWidth - 6) return;
    const startX = event.clientX;
    const startWidth = th.offsetWidth;

    function onMove(moveEvent) {
      th.style.width = `${Math.max(60, startWidth + (moveEvent.clientX - startX))}px`;
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function buildReadonlyField(label, value) {
    const safeLabel = escapeHtml(label);
    const safeValue = escapeHtml(value ?? "-");
    return `
      <div class="readonly-field">
        <label>${safeLabel}</label>
        <input type="text" value="${safeValue}" readonly />
      </div>
    `;
  }

  function setFields(containerId, fields) {
    const container = document.getElementById(containerId);
    if (!container) return;
    if (!fields.length) {
      container.innerHTML = '<div class="readonly-empty">No data available.</div>';
      return;
    }
    container.innerHTML = fields.map((f) => buildReadonlyField(f.label, f.value)).join("");
  }

  function closeRowDetailsModal() {
    const modal = document.getElementById("row-details-modal");
    if (!modal) return;
    
    modal.setAttribute("hidden", "hidden");
    modal.classList.remove("show");
    
    // 2. Destroy modal completely before reopening 
    modal.innerHTML = ""; 
    
    // 4. Remove stale backdrops & CSS locks
    document.body.classList.remove("modal-open");
    document.body.style.removeProperty("overflow");
  }

  function openRowDetailsModal(trigger) {
    const modal = document.getElementById("row-details-modal");
    if (!modal) return;

    let raw = {};
    try {
      raw = JSON.parse(trigger.dataset.raw || "{}");
    } catch {
      raw = {};
    }

    const rowId = trigger.dataset.rowId || "";
    const taskId = trigger.dataset.taskId || "";
    const fmsName = trigger.dataset.fmsName || "";
    const tableName = trigger.dataset.tableName || "";
    const primaryKey = trigger.dataset.primaryKey || "Row ID";

    const title = document.getElementById("row-details-title");
    if (title) title.textContent = `Task Details: ${taskId || rowId || "-"}`;

    const meta = document.getElementById("row-details-meta");
    if (meta) {
      meta.textContent = `${fmsName || "-"} | ${tableName || "-"}`;
    }

    const idFields = [
      { label: "Task ID", value: taskId || rowId || "-" },
    ];

    const pkFields = Object.keys(raw)
      .filter((key) => key && key.toLowerCase().startsWith("pk"))
      .sort((a, b) => a.localeCompare(b))
      .map((key) => ({ label: key, value: raw[key] }));

    const inputFields = Object.keys(raw)
      .filter((key) => key && key.toLowerCase().startsWith("i_"))
      .sort((a, b) => a.localeCompare(b))
      .map((key) => ({ label: key, value: raw[key] }));

    setFields("row-id-fields", idFields);
    setFields("row-pk-fields", pkFields);
    setFields("row-input-fields", inputFields);

    const openTaskLink = document.getElementById("row-open-task-link");
    if (openTaskLink) {
      const fullTaskId = taskId || rowId;
      const params = new URLSearchParams();
      if (tableName) params.set("table_name", tableName);
      if (trigger.dataset.appId) params.set("app_id", trigger.dataset.appId);
      if (primaryKey) params.set("primary_key", primaryKey);
      const query = params.toString();
      openTaskLink.setAttribute(
        "href",
        `/task/${encodeURIComponent(fullTaskId)}${query ? `?${query}` : ""}`
      );
    }

    modal.removeAttribute("hidden");
  }

  document.addEventListener("click", (event) => {
    const closeBtn = event.target.closest("[data-modal-close]");
    if (closeBtn) {
      closeRowDetailsModal();
      return;
    }

    const modalOverlay = event.target.closest("#row-details-modal");
    if (modalOverlay && event.target === modalOverlay) {
      closeRowDetailsModal();
      return;
    }

    const trigger = event.target.closest(".row-id-trigger");
    if (!trigger) return;
    event.preventDefault();
    openRowDetailsModal(trigger);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeRowDetailsModal();
  });

  // ---- Global Attachment Viewer Modal ----------
  document.addEventListener("click", (event) => {
    const btn = event.target.closest(".global-attachment-btn");
    if (!btn) return;
    event.preventDefault();

    const url = btn.dataset.url;
    const title = btn.dataset.title || "Attachment Viewer";
    if (!url) return;

    let modal = document.getElementById("global-attachment-modal");
    if (!modal) {
      // Create the modal dynamically the first time an attachment is clicked
      modal = document.createElement("div");
      modal.id = "global-attachment-modal";
      modal.className = "modal-overlay";
      modal.innerHTML = `
        <div class="modal-card" style="width:min(900px, 96vw); max-height:90vh; display:flex; flex-direction:column; padding:0; overflow:hidden; z-index: 9999;">
          <div class="modal-head" style="padding: 16px 20px; border-bottom: 1px solid var(--border-color); background: var(--bg-surface);">
            <h3 id="global-attachment-title" class="modal-title" style="margin:0; font-size:16px;">Attachment</h3>
            <div style="display:flex; gap:8px;">
              <a id="global-attachment-newtab" href="#" target="_blank" class="btn btn-sm btn-primary">↗ New Tab</a>
              <button type="button" class="btn btn-ghost" data-global-attachment-close>&times;</button>
            </div>
          </div>
          <div style="flex:1; background: var(--bg-body); padding: 16px; overflow: hidden; display: flex; flex-direction: column;">
            <iframe id="global-attachment-frame" src="about:blank" style="width:100%; flex: 1; min-height: 60vh; border:1px solid var(--border-color); border-radius: var(--radius-sm); background: #fff;"></iframe>
          </div>
        </div>
      `;
      // Append to body so it floats above everything
      document.body.appendChild(modal);

      // Close events
      modal.addEventListener("click", (e) => {
        if (e.target.closest("[data-global-attachment-close]") || e.target === modal) {
          modal.setAttribute("hidden", "hidden");
          document.getElementById("global-attachment-frame").src = "about:blank";
        }
      });
      
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !modal.hasAttribute("hidden")) {
          modal.setAttribute("hidden", "hidden");
          document.getElementById("global-attachment-frame").src = "about:blank";
        }
      });
    }

    // Set Data and Show
    document.getElementById("global-attachment-title").textContent = title;
    document.getElementById("global-attachment-newtab").href = url;
    document.getElementById("global-attachment-frame").src = url;
    
    // Ensure it shows completely over other modals by forcing higher z-index
    modal.style.zIndex = "10000";
    modal.removeAttribute("hidden");
  });

})();

// ---- Hidden Fields Toggle (Eye Button) ----------
window.toggleHiddenFields = function(btn) {
  const container = btn.closest('.card') || btn.closest('.modal-card') || document;
  
  const isShowing = btn.classList.contains('is-active');
  const hiddenFields = container.querySelectorAll('.question[data-hidden="true"]');
  
  hiddenFields.forEach(f => {
    if (isShowing) {
      f.style.display = 'none';
    } else {
      f.style.display = ''; 
      f.style.animation = 'fadeSlideIn 0.3s ease';
      f.style.background = 'var(--color-primary-light)';
      setTimeout(() => { f.style.background = 'transparent'; }, 1000);
    }
  });
  
  const textSpan = btn.querySelector('.text');
  if (isShowing) {
    btn.classList.remove('is-active');
    if (textSpan) textSpan.textContent = 'Show Hidden';
  } else {
    btn.classList.add('is-active');
    if (textSpan) textSpan.textContent = 'Hide Fields';
  }
};