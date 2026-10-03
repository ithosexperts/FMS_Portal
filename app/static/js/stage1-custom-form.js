
(function () {
  function parsePickerValues(raw) {
    const text = String(raw || "").trim();
    if (!text) return [];
    return text.split(",").map((value) => value.trim()).filter(Boolean).slice(0, 5);
  }

  function setPickerStatus(message, ok = false) {
    const el = document.getElementById("s1-picker-status");
    if (!el) return;
    el.textContent = message || "";
    el.classList.toggle("is-error", !ok && Boolean(message));
    el.classList.toggle("is-success", ok && Boolean(message));
  }

  function syncPickerHidden() {
    const hidden = document.getElementById("s1-picker-value");
    const rows = document.querySelectorAll("#s1-picker-rows .s1-picker-select");
    if (!hidden) return [];
    const values = [...rows].map((select) => String(select.value || "").trim()).filter(Boolean);
    const unique = [...new Set(values)].slice(0, 5);
    hidden.value = unique.join(", ");
    return unique;
  }

  function updateQtyVisibility() {
    const root = document.getElementById("s1-picker-editor");
    const saved = root && root.dataset.pickerSaved === "true";
    document.querySelectorAll(".stage1-batch-table").forEach((table) => {
      table.classList.toggle("hide-batch-qty", !saved);
    });
  }

  function setPickerSaving(root, saving) {
    if (!root) return;
    root.dataset.pickerSaving = saving ? "true" : "false";
    const availabilityReady = root.dataset.pickerAvailabilityReady === "true";
    const addBtn = document.getElementById("s1-picker-add");
    if (addBtn) addBtn.disabled = saving || root.dataset.locked === "true" || !availabilityReady;
    root.querySelectorAll(".s1-picker-row").forEach((row) => {
      const select = row.querySelector(".s1-picker-select");
      const save = row.querySelector(".s1-picker-save");
      const edit = row.querySelector(".s1-picker-edit");
      const remove = row.querySelector(".s1-picker-remove-one");
      if (saving) {
        if (select) select.disabled = true;
        if (save) save.disabled = true;
        if (edit) edit.disabled = true;
        if (remove) remove.disabled = true;
      } else {
        const savedRow = row.dataset.pickerSaved === "true";
        if (select) select.disabled = savedRow || root.dataset.locked === "true" || !availabilityReady;
        if (save) save.disabled = root.dataset.locked === "true" || !availabilityReady;
        if (edit) edit.disabled = root.dataset.locked === "true" || !availabilityReady;
        if (remove) remove.disabled = root.dataset.locked === "true";
      }
    });
  }

  function validateWeightInput(input) {
    if (!input) return "";
    let value = String(input.value || "").replace(/[^0-9.]/g, "");
    const firstDot = value.indexOf(".");
    if (firstDot >= 0) {
      value = value.slice(0, firstDot + 1) + value.slice(firstDot + 1).replace(/\./g, "");
    }
    if (value.startsWith(".")) value = `0${value}`;
    if (value.includes(".")) {
      const [whole, decimal] = value.split(".");
      value = `${whole}.${decimal.slice(0, 3)}`;
    }
    input.value = value;
    return value;
  }

  function recalcTotalWeight() {
    let total = 0;
    let hasValue = false;
    document.querySelectorAll(".line-weight").forEach((input) => {
      const value = Number(String(input.value || "").trim());
      if (Number.isFinite(value)) {
        total += value;
        hasValue = true;
      }
    });
    const totalInput = document.getElementById("s1-total-weight-kgs");
    if (totalInput) totalInput.value = hasValue ? total.toFixed(3) : "";
  }

  function serializeBatchRows() {
    const rows = Array.from(document.querySelectorAll(".stage1-line-group .stage1-batch-table tbody .batch-row"));
    return rows.map((row) => {
      const packedEl = row.querySelector(".batch-packed");
      const remarksEl = row.querySelector(".batch-remarks");
      return {
        row_id: row.dataset.rowId || "",
        batch_key: row.dataset.batchKey || "",
        line_num: row.closest(".stage1-line-group")?.dataset.lineNum || "",
        packed: Boolean(packedEl && packedEl.checked),
        remarks: remarksEl ? String(remarksEl.value || "").trim() : "",
      };
    });
  }

  function isMandatoryFilled(form) {
    const requiredFields = Array.from(form.querySelectorAll("input[required], select[required], textarea[required]"));
    for (const field of requiredFields) {
      if (field.disabled || field.readOnly) continue;
      if (field.type === "checkbox" && !field.checked) return false;
      if (!String(field.value || "").trim()) return false;
    }

    const root = document.getElementById("s1-picker-editor");
    if (root && root.dataset.locked !== "true") {
      const pickerValues = parsePickerValues(document.getElementById("s1-picker-value")?.value);
      if (root.dataset.pickerSaved !== "true" || pickerValues.length < 1 || pickerValues.length > 5) return false;
    }

    const incompleteWeights = [...document.querySelectorAll(".line-weight")].some((input) => !String(input.value || "").trim());
    if (incompleteWeights) return false;
    return true;
  }

  function areAllPackedChecked() {
    const packed = Array.from(document.querySelectorAll(".stage1-batch-table .batch-row .batch-packed"));
    if (!packed.length) return false;
    return packed.every((item) => item.checked);
  }

  function updateSubmitState() {
    const root = document.querySelector(".stage1-custom");
    const form = document.getElementById("stage-main-form");
    const submitBtn = document.getElementById("btn-submit-stage");
    if (!root || !form || !submitBtn) return;
    const isLocked = root.dataset.stageLocked === "true";
    submitBtn.disabled = !isLocked && !(isMandatoryFilled(form) && areAllPackedChecked());
  }

  function syncBatchPayload() {
    const target = document.getElementById("batch-payload-json");
    if (target) target.value = JSON.stringify(serializeBatchRows());
  }

  async function savePickerSelection(row) {
    const root = document.getElementById("s1-picker-editor");
    const hidden = document.getElementById("s1-picker-value");
    if (!root || !row || !hidden || root.dataset.pickerSaving === "true") return false;
    const select = row.querySelector(".s1-picker-select");
    const saveBtn = row.querySelector(".s1-picker-save");
    if (!select || !String(select.value || "").trim()) {
      setPickerStatus("Select a picker first.");
      return false;
    }

    const allValues = Array.from(document.querySelectorAll("#s1-picker-rows .s1-picker-select"))
      .map((el) => String(el.value || "").trim())
      .filter(Boolean);
    const normalizedValues = allValues.map((value) => value.toLowerCase());
    const duplicate = normalizedValues.some((value, index) => normalizedValues.indexOf(value) !== index);
    if (duplicate) {
      setPickerStatus("The same picker cannot be assigned twice to one PickID.");
      return false;
    }
    const values = [...new Set(allValues)].slice(0, 5);
    if (values.length > 5) {
      setPickerStatus("Maximum 5 pickers allowed.");
      return false;
    }

    const oldText = saveBtn?.textContent || "S";
    setPickerSaving(root, true);
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<span class="s1-inline-spinner" aria-hidden="true"></span> Saving';
      saveBtn.classList.add("is-saving");
    }
    setPickerStatus("Saving picker assignment...");
    try {
      const data = await persistPickerValues(root, values);
      hidden.value = data.value || values.join(", ");
      root.dataset.pickerSaved = String(values.length > 0);
      row.dataset.pickerSaved = "true";
      select.disabled = true;
      if (saveBtn) {
        // The save request is complete: never leave the button showing "Saving".
        saveBtn.classList.remove("is-saving");
        saveBtn.disabled = false;
        saveBtn.textContent = "S";
        saveBtn.hidden = true;
      }
      const edit = row.querySelector(".s1-picker-edit");
      if (edit) edit.hidden = false;
      updateQtyVisibility();
      setPickerStatus("Picker assignment saved.", true);
      updateSubmitState();
      return true;
    } catch (error) {
      if (saveBtn) {
        saveBtn.hidden = false;
        saveBtn.disabled = false;
        saveBtn.textContent = oldText;
      }
      setPickerStatus(error.message || "Picker save failed.");
      return false;
    } finally {
      if (saveBtn) {
        saveBtn.classList.remove("is-saving");
      }
      setPickerSaving(root, false);
      updateQtyVisibility();
      updateSubmitState();
    }
  }

  async function persistPickerValues(root, values) {
    const response = await fetch(root.dataset.pickerSaveUrl, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ pickers: values }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || "Unable to save picker assignment.");
    }
    const saved = parsePickerValues(data.value || "");
    const expected = parsePickerValues(values.join(", "));
    const same = saved.length === expected.length && saved.every((v, i) => v.toLowerCase() === expected[i].toLowerCase());
    if (!same) {
      throw new Error("Picker assignment was not confirmed by the backend.");
    }
    return data;
  }

  function bindPickerControls() {
    const root = document.getElementById("s1-picker-editor");
    const rowsContainer = document.getElementById("s1-picker-rows");
    const addBtn = document.getElementById("s1-picker-add");
    const busyBtn = document.getElementById("s1-busy-picker-btn");
    const busyModal = document.getElementById("s1-busy-picker-modal");
    const busyBody = document.getElementById("s1-busy-picker-body");
    const busyEmpty = document.getElementById("s1-busy-picker-empty");
    const busyError = document.getElementById("s1-busy-picker-error");
    const busyLoading = document.getElementById("s1-busy-picker-loading");
    if (!root || !rowsContainer) return;

    const maxPickers = Math.max(1, Number(root.dataset.maxPickers || 5));
    const locked = root.dataset.locked === "true";
    root.dataset.pickerAvailabilityReady = locked ? "true" : String(root.dataset.pickerAvailabilityReady === "true");

    let pickerOptions = [];
    let busyPickerKeys = new Set();
    let availabilityPromise = null;

    try {
      pickerOptions = JSON.parse(root.dataset.pickerOptions || "[]");
    } catch (_) {
      pickerOptions = [];
    }
    if (!Array.isArray(pickerOptions) || !pickerOptions.length) {
      const first = rowsContainer.querySelector(".s1-picker-select");
      pickerOptions = first ? Array.from(first.options).map((option) => ({
        name: option.value,
        label: option.textContent,
        disabled: option.disabled,
      })) : [];
    }
    pickerOptions = pickerOptions
      .map((option) => ({
        value: String(option?.value ?? option?.name ?? ""),
        label: String(option?.label ?? option?.name ?? option?.value ?? ""),
        disabled: Boolean(option?.disabled),
      }))
      .filter((option) => option.value || option.label);

    const normPicker = (value) => String(value || "").trim().toLowerCase().replace(/[^a-z0-9]/g, "");

    const rebuildSelectOptions = (select) => {
      if (!select) return;
      const selected = String(select.value || "").trim();
      const selectedKey = normPicker(selected);
      const usedByOtherRows = new Set(
        Array.from(rowsContainer.querySelectorAll(".s1-picker-select"))
          .filter((el) => el !== select)
          .map((el) => normPicker(el.value))
          .filter(Boolean)
      );

      select.innerHTML = "";
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Select Picker";
      select.appendChild(placeholder);

      pickerOptions.forEach((option) => {
        const key = normPicker(option.value);
        if (!key) return;
        if (usedByOtherRows.has(key)) return;
        if (root.dataset.pickerAvailabilityReady === "true" && busyPickerKeys.has(key) && key !== selectedKey) return;
        const el = document.createElement("option");
        el.value = option.value;
        el.textContent = option.label || option.value;
        el.disabled = Boolean(option.disabled);
        if (selectedKey && key === selectedKey) el.selected = true;
        select.appendChild(el);
      });
    };

    const rebuildAllSelects = () => {
      rowsContainer.querySelectorAll(".s1-picker-select").forEach(rebuildSelectOptions);
      rowsContainer.querySelectorAll(".s1-picker-row").forEach((row) => {
        const select = row.querySelector(".s1-picker-select");
        const savedRow = row.dataset.pickerSaved === "true";
        if (select) {
          select.disabled = savedRow || locked || root.dataset.pickerAvailabilityReady !== "true";
        }
        const edit = row.querySelector(".s1-picker-edit");
        if (edit) edit.disabled = locked || root.dataset.pickerAvailabilityReady !== "true";
        const save = row.querySelector(".s1-picker-save");
        if (save && row.dataset.pickerSaved !== "true") {
          save.disabled = locked || root.dataset.pickerAvailabilityReady !== "true";
        }
      });
      if (addBtn) {
        const count = rowsContainer.querySelectorAll(".s1-picker-row").length;
        addBtn.disabled = locked || root.dataset.pickerAvailabilityReady !== "true" || count >= maxPickers;
      }
    };

    const setAvailabilityReady = (ready) => {
      root.dataset.pickerAvailabilityReady = ready ? "true" : "false";
      rebuildAllSelects();
      if (ready) setPickerStatus("");
    };

    const fetchBusyPickerBoard = async () => {
      const url = root.dataset.pickerBoardUrl;
      if (!url) return { ok: true, assignments: [], busy_picker_names: [] };
      const params = new URLSearchParams();
      if (root.dataset.currentPickId) params.set("current_pick_id", root.dataset.currentPickId);
      const target = params.toString() ? `${url}?${params.toString()}` : url;
      const response = await fetch(target, {
        method: "GET",
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
        cache: "no-store",
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.error || "Unable to load busy picker board.");
      return data;
    };

    const refreshPickerAvailability = async () => {
      if (locked) {
        setAvailabilityReady(true);
        return;
      }
      if (availabilityPromise) return availabilityPromise;

      root.dataset.pickerAvailabilityReady = "false";
      rebuildAllSelects();
      setPickerStatus("Checking picker availability...");

      availabilityPromise = fetchBusyPickerBoard()
        .then((data) => {
          busyPickerKeys = new Set((data.busy_picker_names || []).map((name) => normPicker(name)).filter(Boolean));
          setAvailabilityReady(true);
          return data;
        })
        .catch((error) => {
          setAvailabilityReady(false);
          setPickerStatus(error.message || "Unable to check picker availability.");
          throw error;
        })
        .finally(() => {
          availabilityPromise = null;
        });
      return availabilityPromise;
    };

    const renderBusyPickerBoard = (assignments) => {
      if (!busyBody) return;
      busyBody.innerHTML = "";
      const rows = Array.isArray(assignments) ? assignments : [];
      if (busyEmpty) busyEmpty.hidden = rows.length !== 0;
      if (!rows.length) return;

      rows.forEach((assignment) => {
        const tr = document.createElement("tr");
        const pickCell = document.createElement("td");
        pickCell.textContent = String(assignment.pick_id || "-");

        const pickerCell = document.createElement("td");
        const names = Array.isArray(assignment.pickers) ? assignment.pickers : [];
        names.forEach((name, index) => {
          if (index) pickerCell.appendChild(document.createTextNode(", "));
          pickerCell.appendChild(document.createTextNode(String(name || "")));
        });

        const rowCell = document.createElement("td");
        const rowLinks = Array.isArray(assignment.row_links) ? assignment.row_links : [];
        rowLinks.forEach((linkInfo, index) => {
          if (index) rowCell.appendChild(document.createTextNode(", "));
          const link = document.createElement("a");
          link.className = "s1-busy-picker-row-link";
          link.href = String(linkInfo.stage_url || "#");
          link.textContent = String(linkInfo.row_id || "-");
          link.title = "Open Stage 1 for this busy Row ID";
          link.target = "_blank";
          link.rel = "noopener";
          rowCell.appendChild(link);
        });

        tr.append(pickCell, pickerCell, rowCell);
        busyBody.appendChild(tr);
      });
    };

    const openBusyPickerModal = async () => {
      if (!busyModal) return;
      busyModal.removeAttribute("hidden");
      if (busyLoading) busyLoading.hidden = false;
      if (busyEmpty) busyEmpty.hidden = true;
      if (busyError) {
        busyError.hidden = true;
        busyError.textContent = "";
      }
      if (busyBody) busyBody.innerHTML = "";
      try {
        const data = await fetchBusyPickerBoard();
        busyPickerKeys = new Set((data.busy_picker_names || []).map((name) => normPicker(name)).filter(Boolean));
        renderBusyPickerBoard(data.assignments || []);
        setAvailabilityReady(true);
      } catch (error) {
        if (busyError) {
          busyError.hidden = false;
          busyError.textContent = error.message || "Unable to load busy picker board.";
        }
      } finally {
        if (busyLoading) busyLoading.hidden = true;
      }
    };

    const closeBusyPickerModal = () => {
      if (busyModal) busyModal.setAttribute("hidden", "hidden");
    };

    const makeRow = () => {
      const row = document.createElement("div");
      row.className = "s1-picker-row";
      row.dataset.pickerSaved = "false";
      row.innerHTML = `<div class="s1-picker-number"></div>`;
      const select = document.createElement("select");
      select.className = "s1-picker-select";
      select.disabled = locked || root.dataset.pickerAvailabilityReady !== "true";

      const used = new Set(
        Array.from(rowsContainer.querySelectorAll(".s1-picker-select"))
          .map((el) => normPicker(el.value))
          .filter(Boolean)
      );
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "Select Picker";
      select.appendChild(placeholder);
      pickerOptions.forEach((option) => {
        if (!option.value) return;
        const key = normPicker(option.value);
        if (used.has(key)) return;
        if (root.dataset.pickerAvailabilityReady === "true" && busyPickerKeys.has(key)) return;
        const el = document.createElement("option");
        el.value = option.value;
        el.textContent = option.label || option.value;
        el.disabled = option.disabled;
        select.appendChild(el);
      });

      const saveBtn = document.createElement("button");
      saveBtn.type = "button";
      saveBtn.className = "btn btn-sm s1-picker-save";
      saveBtn.textContent = "S";
      saveBtn.disabled = locked || root.dataset.pickerAvailabilityReady !== "true";

      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "btn btn-sm btn-ghost s1-picker-edit";
      editBtn.textContent = "✎";
      editBtn.title = "Edit picker";
      editBtn.setAttribute("aria-label", "Edit picker");
      editBtn.hidden = true;
      editBtn.disabled = locked || root.dataset.pickerAvailabilityReady !== "true";

      const removeOneBtn = document.createElement("button");
      removeOneBtn.type = "button";
      removeOneBtn.className = "btn btn-sm btn-ghost s1-picker-remove-one";
      removeOneBtn.textContent = "−";
      removeOneBtn.title = "Remove picker";
      removeOneBtn.setAttribute("aria-label", "Remove picker");
      removeOneBtn.disabled = locked;
      row.append(select, saveBtn, editBtn, removeOneBtn);
      return row;
    };

    const renumber = () => {
      rowsContainer.querySelectorAll(".s1-picker-row").forEach((row, index) => {
        const number = row.querySelector(".s1-picker-number");
        if (number) number.textContent = String(index + 1);
      });
      rebuildAllSelects();
    };

    rowsContainer.addEventListener("change", (event) => {
      const row = event.target.closest(".s1-picker-row");
      if (!row) return;
      if (row.dataset.pickerSaved !== "true") {
        const save = row.querySelector(".s1-picker-save");
        if (save) save.hidden = !String(event.target.value || "").trim();
      }
      rebuildAllSelects();
      const changedSave = row.querySelector(".s1-picker-save");
      if (changedSave && row.dataset.pickerSaved !== "true") {
        changedSave.hidden = !String(event.target.value || "").trim();
      }
      syncPickerHidden();
      updateQtyVisibility();
      updateSubmitState();
    });

    rowsContainer.addEventListener("click", async (event) => {
      const saveBtn = event.target.closest(".s1-picker-save");
      if (saveBtn) {
        event.preventDefault();
        await savePickerSelection(saveBtn.closest(".s1-picker-row"));
        return;
      }

      const editBtn = event.target.closest(".s1-picker-edit");
      if (editBtn) {
        event.preventDefault();
        if (root.dataset.pickerSaving === "true" || root.dataset.pickerAvailabilityReady !== "true") return;
        const row = editBtn.closest(".s1-picker-row");
        if (!row) return;
        row.dataset.pickerSaved = "false";
        const select = row.querySelector(".s1-picker-select");
        const save = row.querySelector(".s1-picker-save");
        rebuildSelectOptions(select);
        if (select) select.disabled = false;
        if (save) {
          save.hidden = !String(select?.value || "").trim();
          save.disabled = false;
          save.textContent = "S";
        }
        editBtn.hidden = true;
        updateQtyVisibility();
        updateSubmitState();
        return;
      }

      const removeOneBtn = event.target.closest(".s1-picker-remove-one");
      if (removeOneBtn) {
        event.preventDefault();
        if (root.dataset.pickerSaving === "true") return;
        const row = removeOneBtn.closest(".s1-picker-row");
        if (!row || locked) return;

        const wasSaved = row.dataset.pickerSaved === "true";
        const removedPicker = String(row.querySelector(".s1-picker-select")?.value || "").trim();
        row.remove();
        if (!rowsContainer.querySelector(".s1-picker-row")) rowsContainer.appendChild(makeRow());
        renumber();

        const values = syncPickerHidden();
        if (wasSaved) {
          setPickerSaving(root, true);
          removeOneBtn.disabled = true;
          removeOneBtn.innerHTML = '<span class="s1-inline-spinner" aria-hidden="true"></span>';
          setPickerStatus("Removing picker assignment...");
          try {
            const data = await persistPickerValues(root, values);
            document.getElementById("s1-picker-value").value = data.value || "";
            root.dataset.pickerSaved = Boolean(data.value);
            setPickerStatus(`${removedPicker || "Picker"} removed.`, true);
            updateQtyVisibility();
          } catch (error) {
            const restored = makeRow();
            const restoredSelect = restored.querySelector(".s1-picker-select");
            if (restoredSelect && removedPicker) {
              restoredSelect.value = removedPicker;
              restored.dataset.pickerSaved = "true";
              restoredSelect.disabled = true;
              const restoredEdit = restored.querySelector(".s1-picker-edit");
              const restoredRemove = restored.querySelector(".s1-picker-remove-one");
              if (restoredEdit) restoredEdit.hidden = false;
              if (restoredRemove) restoredRemove.disabled = false;
            }
            rowsContainer.appendChild(restored);
            renumber();
            syncPickerHidden();
            setPickerStatus(error.message || "Unable to remove picker.");
          } finally {
            removeOneBtn.disabled = root.dataset.locked === "true";
            removeOneBtn.textContent = "−";
            setPickerSaving(root, false);
          }
        }
        updateQtyVisibility();
        updateSubmitState();
        return;
      }
    });

    if (addBtn) {
      addBtn.addEventListener("click", () => {
        const count = rowsContainer.querySelectorAll(".s1-picker-row").length;
        if (locked || root.dataset.pickerAvailabilityReady !== "true" || count >= maxPickers) return;
        rowsContainer.appendChild(makeRow());
        renumber();
        rowsContainer.querySelector(".s1-picker-row:last-child .s1-picker-select")?.focus();
      });
    }

    if (busyBtn && busyModal) {
      busyBtn.addEventListener("click", async (event) => {
        event.preventDefault();
        await openBusyPickerModal();
      });
    }

    document.addEventListener("click", (event) => {
      if (event.target.closest("[data-s1-busy-picker-close]")) closeBusyPickerModal();
      if (event.target === busyModal) closeBusyPickerModal();
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && busyModal && !busyModal.hasAttribute("hidden")) closeBusyPickerModal();
    });

    syncPickerHidden();
    rebuildAllSelects();
    if (!locked) refreshPickerAvailability().catch(() => {});
  }

  async function bindLineWeightControls() {
    const groups = document.querySelectorAll(".stage1-line-group");
    for (const group of groups) {
      const input = group.querySelector(".line-weight");
      const saveBtn = group.querySelector(".line-weight-save");
      const editBtn = group.querySelector(".line-weight-edit");
      if (!input) continue;

      input.addEventListener("input", () => {
        validateWeightInput(input);
        recalcTotalWeight();
        updateSubmitState();
      });

      editBtn?.addEventListener("click", () => {
        const root = document.querySelector(".stage1-custom");
        if (root?.dataset.stageLocked === "true") return;
        input.readOnly = false;
        editBtn.hidden = true;
        if (saveBtn) saveBtn.hidden = false;
        input.focus();
      });

      saveBtn?.addEventListener("click", async () => {
        const value = validateWeightInput(input);
        if (!value || !/^\d+(\.\d{1,3})?$/.test(value)) {
          setPickerStatus("Enter a valid weight up to 3 decimal places.");
          return;
        }
        const oldText = saveBtn.textContent;
        const oldReadOnly = input.readOnly;
        saveBtn.disabled = true;
        input.readOnly = true;
        saveBtn.innerHTML = '<span class="s1-inline-spinner" aria-hidden="true"></span> Saving';
        saveBtn.classList.add("is-saving");
        input.classList.add("is-saving");
        try {
          const stageRoot = document.querySelector(".stage1-custom");
          const saveUrl = stageRoot?.dataset.weightSaveUrl || `/forms/stage/{{ task_id }}/{{ stage_no | urlencode }}/weight-save`;
          const response = await fetch(saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Accept": "application/json" },
            body: JSON.stringify({ row_id: group.dataset.salesRowId || "", weight: value }),
          });
          const data = await response.json().catch(() => ({}));
          if (!response.ok || !data.ok) throw new Error(data.error || "Weight save failed.");
          input.value = Number(value).toFixed(3);
          input.readOnly = true;
          input.dataset.weightSaved = "true";
          saveBtn.hidden = true;
          const editor = group.querySelector(".line-weight-edit");
          if (editor) {
            editor.hidden = document.querySelector(".stage1-custom")?.dataset.stageLocked === "true";
          }
          recalcTotalWeight();
          updateSubmitState();
        } catch (error) {
          input.readOnly = oldReadOnly;
          setPickerStatus(error.message || "Weight save failed.");
        } finally {
          saveBtn.disabled = false;
          saveBtn.textContent = oldText;
          saveBtn.classList.remove("is-saving");
          input.classList.remove("is-saving");
        }
      });
    }
    recalcTotalWeight();
  }

  function bindAttachmentModal() {
    const modal = document.getElementById("attachment-modal");
    const frame = document.getElementById("attachment-frame");
    const title = document.getElementById("attachment-modal-title");
    const openNew = document.getElementById("attachment-open-new");
    const count = document.getElementById("attachment-count");
    const list = document.getElementById("attachment-list");
    const openCurrent = document.getElementById("attachment-current-open-new");
    if (!modal || !frame || !title || !openNew || !list || !openCurrent) {
      return;
    }

    let currentUrl = "";

    function parseUrls(raw) {
      return String(raw || "")
        .split(",")
        .map((part) => part.trim())
        .filter((part) => part);
    }

    function renderAttachmentChips(urls) {
      list.innerHTML = "";
      urls.forEach((url, index) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm";
        btn.textContent = `Attachment ${index + 1}`;
        btn.addEventListener("click", () => {
          currentUrl = url;
          frame.src = currentUrl;
          openCurrent.href = currentUrl;
        });
        list.appendChild(btn);
      });
      if (count) {
        count.textContent = `${urls.length} attachment(s)`;
      }
    }

    document.addEventListener("click", (event) => {
      const trigger = event.target.closest(".attachment-view-btn");
      if (!trigger) {
        return;
      }
      event.preventDefault();
      const urls = parseUrls(trigger.dataset.urls || trigger.dataset.url || "");
      if (!urls.length) {
        return;
      }
      title.textContent = trigger.dataset.title || "Attachment";
      currentUrl = urls[0];
      frame.src = currentUrl;
      openNew.href = urls[0];
      openCurrent.href = currentUrl;
      renderAttachmentChips(urls);
      modal.removeAttribute("hidden");
    });

    document.addEventListener("click", (event) => {
      if (event.target.closest("[data-attachment-close]")) {
        modal.setAttribute("hidden", "hidden");
        frame.src = "about:blank";
        list.innerHTML = "";
        if (count) {
          count.textContent = "";
        }
        return;
      }
      if (event.target === modal) {
        modal.setAttribute("hidden", "hidden");
        frame.src = "about:blank";
        list.innerHTML = "";
        if (count) {
          count.textContent = "";
        }
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !modal.hasAttribute("hidden")) {
        modal.setAttribute("hidden", "hidden");
        frame.src = "about:blank";
        list.innerHTML = "";
        if (count) {
          count.textContent = "";
        }
      }
    });
  }

  function bindPackImageDropzone() {
    const fileInput = document.getElementById("pack-image-1-input");
    const dropzone = document.getElementById("pack-image-1-dropzone");
    const browseBtn = document.getElementById("pack-image-1-browse-btn");
    const list = document.getElementById("pack-image-1-list");
    if (!fileInput || !dropzone || !browseBtn || !list) {
      return;
    }

    const selectedFiles = [];

    const fileFingerprint = (file) => `${file.name}__${file.size}__${file.lastModified}`;

    const syncInputFiles = () => {
      const transfer = new DataTransfer();
      selectedFiles.forEach((f) => transfer.items.add(f));
      fileInput.files = transfer.files;
    };

    const renderList = () => {
      list.innerHTML = "";
      selectedFiles.forEach((file, index) => {
        const chip = document.createElement("span");
        chip.className = "ltg-file-chip";
        chip.textContent = file.name;

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "ltg-chip-remove";
        removeBtn.setAttribute("aria-label", `Remove ${file.name}`);
        removeBtn.textContent = "x";
        removeBtn.addEventListener("click", () => {
          selectedFiles.splice(index, 1);
          syncInputFiles();
          renderList();
        });

        chip.appendChild(removeBtn);
        list.appendChild(chip);
      });
    };

    const appendFiles = (fileList) => {
      const seen = new Set(selectedFiles.map(fileFingerprint));
      Array.from(fileList || []).forEach((file) => {
        if (file && file.name) {
          const key = fileFingerprint(file);
          if (seen.has(key)) {
            return;
          }
          seen.add(key);
          selectedFiles.push(file);
        }
      });
      syncInputFiles();
      renderList();
    };

    browseBtn.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      fileInput.click();
    });

    dropzone.addEventListener("click", (event) => {
      if (event.target && event.target.closest("#pack-image-1-browse-btn")) {
        return;
      }
      fileInput.click();
    });

    fileInput.addEventListener("change", (event) => {
      const pickedFiles = event.target && event.target.files ? event.target.files : fileInput.files;
      appendFiles(pickedFiles);
    });

    dropzone.addEventListener("dragover", (event) => {
      event.preventDefault();
      dropzone.classList.add("drag-over");
    });

    dropzone.addEventListener("dragleave", () => {
      dropzone.classList.remove("drag-over");
    });

    dropzone.addEventListener("drop", (event) => {
      event.preventDefault();
      dropzone.classList.remove("drag-over");
      appendFiles(event.dataTransfer ? event.dataTransfer.files : []);
    });

    const form = document.getElementById("stage-main-form");
    if (form) {
      form.addEventListener("submit", () => {
        syncInputFiles();
      });
    }
  }

  function bindLeadTimeExtensionModal() {
    const openBtn = document.getElementById("btn-lead-time-extension");
    const modal = document.getElementById("lead-time-extension-modal");
    const form = document.getElementById("lead-time-extension-form");
    const dropzone = document.getElementById("ltg-dropzone");
    const fileInput = document.getElementById("ltg-attachments");
    const list = document.getElementById("ltg-attachment-list");
    const browseBtn = document.getElementById("ltg-browse-btn");
    const remarks = document.getElementById("ltg-remarks");
    const requestedMinutes = form ? form.querySelector("input[name='requested_minutes']") : null;
    const saveBtn = document.getElementById("ltg-save-btn");

    // Keep modal open/close functional even if optional attachment controls are missing.
    if (!openBtn || !modal) {
      return;
    }
    if (openBtn.dataset.ltgBound === "true") {
      return;
    }
    openBtn.dataset.ltgBound = "true";

    const selectedFiles = [];

    const autoSizeRemarks = () => {
      if (!remarks) {
        return;
      }
      const lineHeight = 22;
      const minRows = 2;
      const maxRows = 6;
      remarks.style.height = "auto";
      const rows = Math.max(minRows, Math.min(maxRows, Math.ceil(remarks.scrollHeight / lineHeight)));
      remarks.rows = rows;
      remarks.style.height = `${rows * lineHeight}px`;
    };

    const updateLeadTimeSaveState = () => {
      if (!saveBtn) {
        return;
      }
      const minutesRaw = requestedMinutes ? String(requestedMinutes.value || "").trim() : "";
      const remarksRaw = remarks ? String(remarks.value || "").trim() : "";
      const validMinutes = /^\d+$/.test(minutesRaw) && Number(minutesRaw) > 0;
      const validRemarks = remarksRaw.length > 0;
      saveBtn.disabled = !(validMinutes && validRemarks);
    };

    const syncInputFiles = () => {
      if (!fileInput) {
        return;
      }
      const transfer = new DataTransfer();
      selectedFiles.forEach((f) => transfer.items.add(f));
      fileInput.files = transfer.files;
    };

    const renderFileChips = () => {
      if (!list) {
        return;
      }
      list.innerHTML = "";
      selectedFiles.forEach((file, index) => {
        const chip = document.createElement("span");
        chip.className = "ltg-file-chip";
        chip.textContent = file.name;

        const removeBtn = document.createElement("button");
        removeBtn.type = "button";
        removeBtn.className = "ltg-chip-remove";
        removeBtn.setAttribute("aria-label", `Remove ${file.name}`);
        removeBtn.textContent = "x";
        removeBtn.addEventListener("click", () => {
          selectedFiles.splice(index, 1);
          syncInputFiles();
          renderFileChips();
        });

        chip.appendChild(removeBtn);
        list.appendChild(chip);
      });
    };

    const appendFiles = (fileList) => {
      const incoming = Array.from(fileList || []);
      incoming.forEach((file) => {
        if (!file || !file.name) {
          return;
        }
        selectedFiles.push(file);
      });
      syncInputFiles();
      renderFileChips();
    };

    const openModal = () => {
      modal.removeAttribute("hidden");
      autoSizeRemarks();
      updateLeadTimeSaveState();
    };

    const closeModal = () => {
      modal.setAttribute("hidden", "hidden");
    };

    openBtn.addEventListener("click", openModal);

    document.addEventListener("click", (event) => {
      if (event.target.closest("[data-ltg-close]")) {
        closeModal();
      }
      if (event.target === modal) {
        closeModal();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !modal.hasAttribute("hidden")) {
        closeModal();
      }
    });

    if (browseBtn && fileInput) {
      browseBtn.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        fileInput.click();
      });
    }

    if (fileInput) {
      fileInput.addEventListener("change", () => {
        appendFiles(fileInput.files);
        updateLeadTimeSaveState();
      });
    }

    if (dropzone && fileInput) {
      dropzone.addEventListener("click", (event) => {
        if (event.target && event.target.closest("#ltg-browse-btn")) {
          return;
        }
        fileInput.click();
      });

      dropzone.addEventListener("dragover", (event) => {
        event.preventDefault();
        dropzone.classList.add("drag-over");
      });

      dropzone.addEventListener("dragleave", () => {
        dropzone.classList.remove("drag-over");
      });

      dropzone.addEventListener("drop", (event) => {
        event.preventDefault();
        dropzone.classList.remove("drag-over");
        appendFiles(event.dataTransfer ? event.dataTransfer.files : []);
      });
    }

    if (remarks) {
      remarks.addEventListener("input", () => {
        autoSizeRemarks();
        updateLeadTimeSaveState();
      });
      autoSizeRemarks();
    }

    if (requestedMinutes) {
      requestedMinutes.addEventListener("input", updateLeadTimeSaveState);
      requestedMinutes.addEventListener("change", updateLeadTimeSaveState);
    }

    updateLeadTimeSaveState();

    if (form) {
      form.addEventListener("submit", () => {
        syncInputFiles();
        if (saveBtn && !saveBtn.disabled) {
          saveBtn.disabled = true;
          saveBtn.dataset.originalText = saveBtn.textContent || "Save";
          saveBtn.textContent = "Processing...";
        }
      });
    }
  }

  function initStage1CustomFormPage() {
    bindAttachmentModal();
    bindLeadTimeExtensionModal();
    bindPackImageDropzone();
    bindPickerControls();
    bindLineWeightControls();

    const form = document.getElementById("stage-main-form");
    if (!form) {
      return;
    }

    syncBatchPayload();
    updateSubmitState();

    form.addEventListener("input", () => {
      syncBatchPayload();
      updateSubmitState();
    });

    form.addEventListener("change", () => {
      syncBatchPayload();
      updateSubmitState();
    });

    form.addEventListener("submit", () => {
      syncBatchPayload();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initStage1CustomFormPage);
  } else {
    initStage1CustomFormPage();
  }
})();
