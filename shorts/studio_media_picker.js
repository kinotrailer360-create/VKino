/* Vanilla JS media picker for the existing authenticated KinoMir360 Site.
 * Usage:
 *   const picker = window.VKinoMediaPicker.mount({
 *      element: document.querySelector("#vkino-media"),
 *      apiBase: "https://your-vkino-host/api/studio/v1",
 *      token: currentSessionToken,
 *      selected: project.mediaLibrary || [],
 *      onChange: ids => { project.mediaLibrary = ids; project.revision++; }
 *   });
 *
 * Tokens are never put in a query string or persisted by this widget.
 * The host Site keeps ownership of session, project persistence and stage approval.
 */
(function (global) {
  "use strict";
  const MAX = 20;

  function element(tag, attrs, label) {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (key === "className") e.className = value;
      else if (key === "type") e.type = value;
      else e.setAttribute(key, value);
    });
    if (label !== undefined) e.textContent = label;
    return e;
  }

  function mount(config) {
    const root = config.element;
    if (!root || typeof config.onChange !== "function") {
      throw new Error("VKinoMediaPicker needs an element and onChange callback");
    }
    const base = String(config.apiBase || "").replace(/\/$/, "");
    const token = String(config.token || "");
    if (!base.startsWith("https://") || !token) throw new Error("Secure API session required");

    const selected = new Set((config.selected || []).slice(0, MAX));
    const pane = element("section", { className: "vkino-media-picker" });
    const title = element("h3", {}, "📁 Материалы для монтажа");
    const info = element("p", {}, "Выберите до 20 фото, GIF и видео. Поиск фрагментов выполнит VKino.");
    const picker = element("input", { type: "file", multiple: "",
      accept: "image/jpeg,image/png,image/webp,image/gif,image/bmp,image/tiff,video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo,.mkv,.avi,.m4v" });
    const upload = element("button", { type: "button" }, "Загрузить выбранные файлы");
    const status = element("p", { role: "status" }, "");
    const list = element("div", { className: "vkino-media-list" });
    pane.append(title, info, picker, upload, status, list);
    root.replaceChildren(pane);
    let items = [];

    function redraw() {
      list.replaceChildren();
      for (const item of items) {
        const line = element("label", {});
        const choice = element("input", { type: "checkbox" });
        choice.checked = selected.has(item.id);
        choice.addEventListener("change", () => {
          if (choice.checked && selected.size >= MAX) {
            choice.checked = false;
            status.textContent = "Не больше 20 файлов для одного проекта.";
            return;
          }
          if (choice.checked) selected.add(item.id);
          else selected.delete(item.id);
          config.onChange(Array.from(selected));
          status.textContent = "Выбрано файлов: " + selected.size;
        });
        const name = element("span", {}, " " + String(item.name || "media") +
          " (" + Math.round((item.size || 0) / 1024) + " КБ)");
        line.append(choice, name);
        list.append(line);
      }
    }

    async function request(path, options) {
      const response = await fetch(base + path, Object.assign({}, options || {}, {
        headers: Object.assign({}, options && options.headers, { Authorization: "Bearer " + token }),
        cache: "no-store"
      }));
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Ошибка Studio: HTTP " + response.status);
      return data;
    }

    async function refresh() {
      const data = await request("/files");
      items = data.files || [];
      redraw();
      return items;
    }

    upload.addEventListener("click", async () => {
      const files = Array.from(picker.files || []);
      if (!files.length) { status.textContent = "Сначала выберите файлы."; return; }
      if (files.length > MAX) { status.textContent = "За один раз — максимум 20 файлов."; return; }
      upload.disabled = true;
      status.textContent = "Загружаю " + files.length + " файлов…";
      try {
        const body = new FormData();
        for (const file of files) body.append("file", file, file.name);
        const result = await request("/files/batch", { method: "POST", body });
        for (const item of (result.files || [])) if (selected.size < MAX) selected.add(item.id);
        config.onChange(Array.from(selected));
        await refresh();
        status.textContent = "Загружено: " + (result.files || []).length +
          ". Ошибок: " + (result.errors || []).length +
          (result.errors || []).map(e => " " + e.name + ": " + e.error).join("; ");
        picker.value = "";
      } catch (error) {
        status.textContent = String(error.message || error);
      } finally {
        upload.disabled = false;
      }
    });

    refresh().catch(error => { status.textContent = String(error.message || error); });
    return { refresh, getSelected: () => Array.from(selected), destroy: () => root.replaceChildren() };
  }

  global.VKinoMediaPicker = Object.freeze({ mount });
})(window);
